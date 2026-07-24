"""Recover equations, tables and figure descriptions from already-extracted crop PNGs.

Normal conversion runs ``pymupdf4llm.to_markdown(..., write_images=True)``, which pulls every
vector-drawn display equation, table and figure out of the PDF into its own PNG and leaves an
opaque ``![](...png)`` placeholder behind in the Markdown. The notation is then absent from the
text layer entirely -- invisible to full-text search.

Those crops are already isolated regions, so this module does not re-render or re-extract
anything. It walks the converted Markdown, decides what each referenced crop is, asks a locally
served OCR model the matching question, and splices the answer back at the placeholder's
position.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import struct
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ._atomic import replace_with_retry
from ._ollama_client import OllamaError, generate, probe
from .artifacts import (
    ArtifactError,
    current_generation_jsonl,
    resolve_reader_db_path,
    stage_and_publish,
    write_jsonl_upserting_record,
)
from .config import ImageOcrSettings
from .converter import _with_front_matter
from .fts import get_item_context
from .identity import MARKDOWN_IMAGE_RE, strip_front_matter
from .indexer import TextIndexRecord, _sha256, load_indexed_keys
from .lock import PipelineLockedError, pipeline_write_lock

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

OCR_TOOL = "glm-ocr"

# Class names this module classifies crops into. "skip" means never send the crop to the model.
CLASS_SKIP = "skip"
CLASS_FORMULA = "formula"
CLASS_TABLE = "table"
CLASS_FIGURE = "figure"

# GLM-OCR is prompted per task rather than given a generic instruction; the task prefix is what
# selects formula/table/figure behaviour in the model.
TASK_PROMPTS = {
    CLASS_FORMULA: "Formula Recognition:",
    CLASS_TABLE: "Table Recognition:",
    CLASS_FIGURE: "Figure Recognition:",
}

# Thresholds measured from real converted output (aspect = width / height):
#   - display equations are wide, short strips, well above EQUATION_MIN_ASPECT
#   - figures and plots cluster inside FIGURE_ASPECT_RANGE
#   - decorative page furniture (rules, spine bars) is far below FURNITURE_MAX_ASPECT
# These three bands do not partition the space; see classify_crop for the gap between them.
EQUATION_MIN_ASPECT = 5.0
FIGURE_ASPECT_RANGE = (0.4, 1.5)
FURNITURE_MAX_ASPECT = 0.1

# Crops smaller than this in either dimension carry no readable content at typical render DPI.
MIN_READABLE_PIXELS = 8

# Compressed PNG bytes per pixel: a proxy for visual complexity that needs no image library.
# A solid or near-solid crop (a rule, a spine bar, a logo block) compresses to almost nothing,
# while glyphs or a plot do not. Measured on the validation corpus, decoration sat at <= 0.024
# and real content at >= 0.068 with a clean gap between; 0.04 splits them. This is the one signal
# that separates a solid decorative bar from a wide display equation, which are indistinguishable
# by aspect ratio or surrounding text.
SKIP_COMPLEXITY_BYTES_PER_PIXEL = 0.04

# Above this aspect ratio a crop is a single-line equation strip, not a block. A "Table 3" or
# "Figure 4" appearing beside such a strip is a running-prose cross-reference, not a caption for
# it -- observed repeatedly in real documents, where thin equations sit next to sentences like
# "Table 4 shows the coefficients". Captions are only trusted for blockier crops.
CAPTION_MAX_ASPECT = 8.0

# Captions in converted output are usually emphasised -- "**Fig. 3** Plots showing..." -- so the
# leading Markdown markers have to be skipped before the label itself can match.
_CAPTION_LEAD = r"^[\s*_#>]*"
CAPTION_FIGURE_RE = re.compile(_CAPTION_LEAD + r"(?:figure|fig\.?|abb\.?|abbildung)\s*\d", re.IGNORECASE)
CAPTION_TABLE_RE = re.compile(_CAPTION_LEAD + r"(?:table|tab\.?|tabelle)\s*\d", re.IGNORECASE)

# pymupdf4llm annotates text it recovered from inside a picture region with this HTML comment.
# It is a direct statement from the extractor that the neighbouring crop is a picture, which no
# geometric heuristic can match for reliability -- crops it marks are figures regardless of shape.
PICTURE_TEXT_MARKER = "<!-- Start of picture text -->"

# Front-matter key recording that a document has already been enriched. Its presence is the
# re-run guard: formula splices are idempotent (the placeholder is consumed) but figure splices
# are not, so a second unguarded pass would append the same description again.
FRONT_MATTER_TOOL_KEY = "image_ocr_tool"
FRONT_MATTER_AT_KEY = "image_ocr_at"

CACHE_FILENAME = ".image-ocr-cache.json"

# Bumped whenever the *meaning* of a cached entry changes without the crop bytes, prompt or model
# changing -- e.g. a change to sanitize_ocr_output. A key mismatch then forces a clean re-OCR
# instead of serving output produced under the old behaviour. The model is part of the key too, so
# switching image_ocr.model correctly misses rather than reusing another model's answer.
CACHE_SCHEMA_VERSION = "1"

# A single crop should never contribute more than this much text. A runaway table or a model
# that starts repeating itself would otherwise be spliced verbatim into the document.
MAX_OCR_RESPONSE_CHARS = 8000


@dataclass(frozen=True)
class CropRef:
    """One ``![](...)`` reference in the Markdown body, resolved against the images directory."""

    span: tuple[int, int]
    markup: str
    link: str
    png_path: Path | None
    width: int
    height: int
    text_before: str
    text_after: str
    byte_size: int = 0

    @property
    def exists(self) -> bool:
        return self.png_path is not None and self.width > 0 and self.height > 0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def complexity(self) -> float:
        """Compressed PNG bytes per pixel -- a dependency-free proxy for visual complexity."""
        pixels = self.width * self.height
        return self.byte_size / pixels if pixels else 0.0


def read_png_size(path: Path) -> tuple[int, int] | None:
    """Return (width, height) from a PNG's IHDR chunk, or None if it isn't a readable PNG.

    Reads 24 bytes rather than decoding the image: dimensions are all the classifier needs, and
    this keeps the module free of any image-library dependency.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    if width == 0 or height == 0:
        return None
    return width, height


def link_basename(link: str) -> str:
    """Extract a filename from a Markdown image target, whatever shape it arrives in.

    Converted Markdown in the wild carries absolute paths written by whichever machine ran the
    conversion, so the directory part is meaningless here and only the filename is used. Both
    separators are normalised explicitly: ``Path(r"C:\\a\\b.png").name`` returns the entire
    string on POSIX, so relying on pathlib alone would silently fail on non-Windows CI.
    """
    target = link.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    # Markdown allows an optional title after the path: ![](path.png "Title")
    match = re.match(r"^(.*?)\s+[\"'].*[\"']$", target, re.DOTALL)
    if match:
        target = match.group(1).strip()
    target = urllib.parse.unquote(target)
    return target.replace("\\", "/").rsplit("/", 1)[-1]


def _link_target(markup: str) -> str:
    """Pull the target out of ``![alt](target)``.

    MARKDOWN_IMAGE_RE has no capture group -- it exists to *strip* image syntax elsewhere in the
    project -- so the target is sliced off group(0) rather than captured.
    """
    open_paren = markup.index("](") + 2
    return markup[open_paren:-1]


def _neighbouring_line(body: str, start: int, end: int, *, forward: bool) -> str:
    """Return the nearest non-blank line after (or before) a span, for caption detection."""
    segment = body[end:] if forward else body[:start]
    lines = segment.split("\n")
    ordered = lines if forward else reversed(lines)
    for line in ordered:
        stripped = line.strip()
        if stripped and not MARKDOWN_IMAGE_RE.fullmatch(stripped):
            return stripped
    return ""


def find_crop_refs(body: str, images_dir: Path) -> list[CropRef]:
    """Locate every image reference in the Markdown body and resolve it against images_dir.

    Resolution is by filename against the images directory derived from the Markdown's own
    location, never by the path embedded in the link: those paths are absolute and frequently
    belong to a different machine than the one running this command.
    """
    refs: list[CropRef] = []
    for match in MARKDOWN_IMAGE_RE.finditer(body):
        markup = match.group(0)
        link = _link_target(markup)
        basename = link_basename(link)
        png_path: Path | None = None
        width = height = 0
        byte_size = 0
        if basename:
            candidate = images_dir / basename
            size = read_png_size(candidate)
            if size is not None:
                png_path = candidate
                width, height = size
                try:
                    byte_size = candidate.stat().st_size
                except OSError:
                    byte_size = 0
        refs.append(
            CropRef(
                span=match.span(),
                markup=markup,
                link=link,
                png_path=png_path,
                width=width,
                height=height,
                text_before=_neighbouring_line(body, match.start(), match.end(), forward=False),
                text_after=_neighbouring_line(body, match.start(), match.end(), forward=True),
                byte_size=byte_size,
            )
        )
    return refs


def classify_crop(ref: CropRef, *, has_math: bool) -> str:
    """Decide what a crop is, which selects the OCR task prompt used to read it.

    Return one of CLASS_SKIP, CLASS_FORMULA, CLASS_TABLE or CLASS_FIGURE.

    Available signals, all cheap and already gathered on ``ref``:
      - ``ref.width`` / ``ref.height`` / ``ref.aspect`` -- read from the PNG header. Absolute
        size discriminates where aspect alone cannot: a wide-ish crop a few hundred pixels across
        is usually a multi-line equation, while one several hundred pixels tall is usually a plot.
      - ``ref.text_after`` / ``ref.text_before`` -- the nearest non-blank Markdown lines.
        PICTURE_TEXT_MARKER appearing there is the extractor's own assertion that the crop is a
        picture, and is the single most reliable signal available. CAPTION_FIGURE_RE and
        CAPTION_TABLE_RE match "Fig. 3" / "Table 2" style labels in English and German, including
        through the emphasis markers converted output wraps them in.
      - ``has_math`` -- whether the document as a whole was detected as containing mathematics.

    The measured bands (EQUATION_MIN_ASPECT, FIGURE_ASPECT_RANGE, FURNITURE_MAX_ASPECT) are
    landmarks, not a partition: a substantial share of real crops land in the gap between the
    figure band and the equation band. Those middle cases are the interesting ones -- multi-line
    display equations, inline fragments and small tables all live there, and each wants a
    different prompt. Sending the wrong prompt does not fail loudly; it returns confident,
    well-formed, wrong output.

    Trade-off to weigh: skipping aggressively is cheap and safe against garbage, but silently
    drops real content that then stays unsearchable. Classifying permissively recovers more but
    spends a model call per crop and risks mis-prompted output being spliced into the document.

    Use --dry-run to see how any rule here partitions a real document before running any OCR.
    """
    if not ref.exists:
        return CLASS_SKIP
    if ref.width < MIN_READABLE_PIXELS or ref.height < MIN_READABLE_PIXELS:
        return CLASS_SKIP

    # Decoration first, by compression: a solid rule, spine bar or logo block carries almost no
    # bytes per pixel. This is the only signal that separates a solid bar from a wide display
    # equation, which look identical by aspect ratio and have no distinguishing caption.
    if ref.complexity < SKIP_COMPLEXITY_BYTES_PER_PIXEL:
        return CLASS_SKIP

    before, after = ref.text_before, ref.text_after

    # The extractor's own picture annotation is definitive: it marks a crop as a picture region
    # regardless of shape, and is what separates a gap-band figure from a gap-band equation.
    if PICTURE_TEXT_MARKER in before or PICTURE_TEXT_MARKER in after:
        return CLASS_FIGURE

    # A caption is only trusted for a blockier crop. Beside a thin single-line equation strip, a
    # "Table 3" / "Figure 4" mention is running-prose cross-reference, not a caption -- so it must
    # not pull an equation into the table or figure path.
    if ref.aspect <= CAPTION_MAX_ASPECT:
        if CAPTION_FIGURE_RE.search(before) or CAPTION_FIGURE_RE.search(after):
            return CLASS_FIGURE
        if CAPTION_TABLE_RE.search(before) or CAPTION_TABLE_RE.search(after):
            return CLASS_TABLE

    # Uncaptioned crops in the figure aspect band are plots; everything else -- wide equation
    # strips and the ambiguous middle band alike -- defaults to formula, since display equations
    # dominate real math documents and an equation prompt on a stray non-equation still yields
    # empty or harmless output rather than a corrupting mis-splice.
    low, high = FIGURE_ASPECT_RANGE
    if low <= ref.aspect <= high:
        return CLASS_FIGURE
    return CLASS_FORMULA


def sanitize_ocr_output(text: str, *, max_chars: int = MAX_OCR_RESPONSE_CHARS) -> str:
    """Make a model response safe to splice into converted Markdown.

    Strips any image syntax the model emits. Such a reference would point at nothing, and worse,
    a later run would discover it, try to resolve it against the images directory, and treat the
    miss as a missing crop. Also caps length so one runaway response cannot dominate a document.

    A bare ``---`` line needs no handling: front-matter parsing only strips the *leading* block,
    so a horizontal rule in the body is inert.
    """
    cleaned = MARKDOWN_IMAGE_RE.sub("", text).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "\n\n<!-- ocr output truncated -->"
    return cleaned


def render_replacement(crop_class: str, ocr_text: str, markup: str) -> str:
    """Build the Markdown that replaces (or augments) one image reference.

    Figures keep their image link and gain a description beneath it: a plot's pixels carry
    information no caption reproduces, so the image stays and the text makes it searchable.
    Equations and tables replace the placeholder outright -- the crop was only ever a stand-in
    for notation the text layer failed to keep.
    """
    if not ocr_text:
        return markup
    if crop_class == CLASS_FORMULA:
        return f"$$\n{ocr_text}\n$$"
    if crop_class == CLASS_TABLE:
        return ocr_text
    if crop_class == CLASS_FIGURE:
        return f"{markup}\n\n{ocr_text}"
    return markup


def splice(body: str, replacements: list[tuple[tuple[int, int], str]]) -> str:
    """Apply replacements to the body, back to front so earlier spans stay valid."""
    result = body
    for (start, end), text in sorted(replacements, key=lambda item: item[0][0], reverse=True):
        result = result[:start] + text + result[end:]
    return result


def front_matter_fields(markdown: str) -> dict[str, str]:
    """Read the leading YAML front-matter block as flat key -> unquoted-value pairs.

    Only used to detect prior enrichment; the record fields themselves come from the index, not
    from parsing the document.
    """
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return {}
    fields: dict[str, str] = {}
    for line in markdown[4:end].split("\n"):
        key, separator, value = line.partition(":")
        if separator and key.strip():
            fields[key.strip()] = value.strip().strip('"')
    return fields


def resolve_under_output_root(stored: Path, output_root: Path) -> Path | None:
    """Locate a recorded output file, re-rooting it under output_root if the stored path is stale.

    Index records store absolute paths written by whichever machine ran the conversion. A library
    that has since moved -- a new machine, a renamed sync folder -- keeps working for search,
    because the text lives in the index, but every recorded path stops resolving on disk.

    Rather than guess at a rewrite rule, this tries progressively shorter tails of the stored path
    against the configured output root and returns the first that exists. The longest match wins,
    so the deepest unambiguous suffix is used. Returns None when nothing matches, which the caller
    reports rather than silently operating on the wrong file.
    """
    if stored.exists():
        return stored
    parts = stored.parts
    # Skip index 0: on Windows that is the drive anchor, which never appears mid-tree.
    for start in range(1, len(parts)):
        candidate = output_root.joinpath(*parts[start:])
        if candidate.exists():
            return candidate
    return None


def source_path_for(indexed: Path, suffix: str) -> Path:
    """Return the original converted file for an indexed path, undoing any enrichment suffix.

    The index may point at either the pristine original (before the first enrichment) or the
    enriched sibling (after it). Enrichment always regenerates from the original, so this strips
    the suffix when present to recover it. With an empty suffix, enrichment overwrites in place
    and the indexed path is already the source.
    """
    if suffix and indexed.stem.endswith(suffix):
        return indexed.with_name(indexed.stem[: -len(suffix)] + indexed.suffix)
    return indexed


def enriched_path_for(source: Path, suffix: str) -> Path:
    """Return the sibling file enrichment writes to, leaving the source untouched.

    An empty suffix means "overwrite the source in place" -- then this is the source itself.
    """
    if not suffix:
        return source
    return source.with_name(source.stem + suffix + source.suffix)


def composite_extraction_tool(previous: str) -> str:
    """Append the OCR marker to an extraction tool label without stacking duplicates.

    Keeping the original extractor visible matters: enrichment reads crops the original
    extractor produced, it does not replace it, and provenance should still say so.
    """
    base = previous.split("+", 1)[0] if previous else ""
    return f"{base}+{OCR_TOOL}" if base else OCR_TOOL


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class StaleMarkdownError(RuntimeError):
    """The Markdown changed on disk while OCR was running, so the splice is out of date."""


@dataclass(frozen=True)
class CropPlan:
    """A crop, the class decided for it, and (after OCR) what came back."""

    ref: CropRef
    crop_class: str
    ocr_text: str = ""
    error: str = ""


@dataclass
class ImageOcrResult:
    ok: bool
    attachment_key: str
    markdown_path: str = ""
    enriched_markdown_path: str = ""
    images_dir: str = ""
    total_refs: int = 0
    missing_pngs: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    ocr_succeeded: int = 0
    ocr_failed: int = 0
    enriched_at: str = ""
    dry_run: bool = False
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def plan_crops(
    body: str, images_dir: Path, *, has_math: bool, tolerate_unimplemented: bool = False
) -> list[CropPlan]:
    """Classify every crop reference in a body, without contacting the OCR model.

    ``tolerate_unimplemented`` lets --dry-run stay useful before classify_crop() has a rule:
    the measurements are what a contributor needs in order to write one, so refusing to show
    them until the rule exists would be exactly backwards.
    """
    plans: list[CropPlan] = []
    for ref in find_crop_refs(body, images_dir):
        try:
            crop_class = classify_crop(ref, has_math=has_math)
            error = ""
        except NotImplementedError:
            if not tolerate_unimplemented:
                raise
            crop_class = "?"
            error = "classify_crop() not implemented"
        plans.append(CropPlan(ref=ref, crop_class=crop_class, error=error))
    return plans


def ocr_images_for_attachment(
    attachment_key: str,
    *,
    index_root: Path,
    lock_root: Path,
    output_root: Path,
    settings: ImageOcrSettings,
    force: bool = False,
    dry_run: bool = False,
    plans_out: list[CropPlan] | None = None,
) -> ImageOcrResult:
    """Read one attachment's extracted crops with a local OCR model and enrich its Markdown.

    Writes only converted-text output (the Markdown file and a published index generation) and
    never touches the source PDF, the Zotero database, or the extracted crops themselves.
    """
    try:
        jsonl_path = current_generation_jsonl(index_root)
    except ArtifactError as exc:
        return ImageOcrResult(ok=False, attachment_key=attachment_key, error=str(exc))
    if jsonl_path is None:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            error=(
                "Image OCR publishes a managed index generation, but no managed generation "
                "exists yet. Publish one with 'zotero-pdf-text rebuild-index', then retry."
            ),
        )
    try:
        db_path = resolve_reader_db_path(index_root / "zotero_text_index.sqlite")
    except ArtifactError as exc:
        return ImageOcrResult(ok=False, attachment_key=attachment_key, error=str(exc))

    records = get_item_context(db_path, attachment_key=attachment_key).get("records", [])
    if not records:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            error=f"No indexed record found for attachment key {attachment_key}",
        )
    if attachment_key not in load_indexed_keys(jsonl_path):
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            error=f"No text-sidecar record found for attachment key {attachment_key}",
        )
    record = records[0]

    stored_markdown_path = Path(record["markdown_path"])
    resolved = resolve_under_output_root(stored_markdown_path, output_root)
    if resolved is None:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(stored_markdown_path),
            error=(
                f"Converted Markdown not found. The index records it at {stored_markdown_path}, "
                f"which does not exist, and no matching file was found under {output_root}."
            ),
        )
    # Enrichment always regenerates from the pristine original, even when the index already points
    # at a previously enriched sibling -- so recover the source and re-resolve it under the tree.
    suffix = settings.enriched_suffix
    source_candidate = source_path_for(resolved, suffix)
    source_path = resolve_under_output_root(source_candidate, output_root)
    if source_path is None:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(source_candidate),
            error=f"Original converted Markdown not found for {source_candidate}.",
        )
    enriched_path = enriched_path_for(source_path, suffix)
    # Images are derived from the original's stem and shared by both files; never from the
    # enriched stem, which has no images directory of its own.
    images_dir = source_path.parent.parent / "images" / source_path.stem

    if enriched_path != source_path and enriched_path.exists() and not force and not dry_run:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(source_path),
            enriched_markdown_path=str(enriched_path),
            images_dir=str(images_dir),
            error=(
                f"An enriched copy already exists at {enriched_path}. The original is untouched; "
                f"pass --force to regenerate the enriched copy from it."
            ),
        )

    original_bytes = source_path.read_bytes()
    raw_markdown = original_bytes.decode("utf-8")
    fields = front_matter_fields(raw_markdown)
    if fields.get(FRONT_MATTER_TOOL_KEY) and enriched_path == source_path and not force and not dry_run:
        # In-place mode (empty suffix): the source itself carries the enrichment marker.
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(source_path),
            images_dir=str(images_dir),
            error=(
                f"This attachment was already enriched with image OCR at "
                f"{fields.get(FRONT_MATTER_AT_KEY, 'an earlier time')}. Re-running would append "
                f"figure descriptions a second time. Pass --force to redo it anyway."
            ),
        )

    markdown_path = source_path
    body = strip_front_matter(raw_markdown)
    has_math = bool(record.get("has_math", False))
    try:
        plans = plan_crops(body, images_dir, has_math=has_math, tolerate_unimplemented=dry_run)
    except NotImplementedError as exc:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(markdown_path),
            images_dir=str(images_dir),
            error=f"{exc} -- implement it, or use --dry-run to inspect the crops first.",
        )

    missing = sum(1 for plan in plans if not plan.ref.exists)
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.crop_class] = counts.get(plan.crop_class, 0) + 1

    if dry_run:
        if plans_out is not None:
            plans_out.extend(plans)
        return ImageOcrResult(
            ok=True,
            attachment_key=attachment_key,
            markdown_path=str(markdown_path),
            enriched_markdown_path=str(enriched_path),
            images_dir=str(images_dir),
            total_refs=len(plans),
            missing_pngs=missing,
            counts=counts,
            dry_run=True,
        )

    status = probe(settings.base_url, settings.model)
    if not status.ok:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(markdown_path),
            images_dir=str(images_dir),
            error=status.detail,
        )

    plans = _run_ocr(plans, settings=settings, images_dir=images_dir)
    if plans_out is not None:
        plans_out.extend(plans)
    succeeded = sum(1 for plan in plans if plan.ocr_text)
    failed = sum(1 for plan in plans if plan.error and plan.crop_class != CLASS_SKIP)

    replacements = [
        (plan.ref.span, render_replacement(plan.crop_class, plan.ocr_text, plan.ref.markup))
        for plan in plans
        if plan.ocr_text
    ]
    if not replacements:
        # Nothing to OCR and nothing that failed is a legitimate no-op, not an error: a document
        # whose crops are all decorative, or one whose equation placeholders were already
        # consumed by an earlier pass, has no work left to do.
        eligible = sum(1 for plan in plans if plan.crop_class in TASK_PROMPTS and plan.ref.exists)
        return ImageOcrResult(
            ok=eligible == 0,
            attachment_key=attachment_key,
            markdown_path=str(markdown_path),
            images_dir=str(images_dir),
            total_refs=len(plans),
            missing_pngs=missing,
            counts=counts,
            ocr_failed=failed,
            note="" if eligible else "No crop needed OCR; the Markdown was left unchanged.",
            error=(
                ""
                if eligible == 0
                else f"All {eligible} eligible crops failed OCR; the Markdown was left unchanged."
            ),
        )

    # Math-capability provenance is only claimed when the mathematics was actually recovered:
    # every formula crop that was eligible produced text. A figure-only run, or one where any
    # formula crop failed or came back empty, leaves the extractor label unchanged so the
    # `math_extraction_may_be_lossy` warning (gated on it) correctly persists. That "GLM-OCR
    # participated" is still recorded, separately, in the `image_ocr_tool` front-matter field.
    formula_plans = [plan for plan in plans if plan.crop_class == CLASS_FORMULA]
    math_recovered = bool(formula_plans) and all(plan.ocr_text for plan in formula_plans)
    previous_tool = str(record.get("extraction_tool", ""))
    extraction_tool = composite_extraction_tool(previous_tool) if math_recovered else previous_tool

    enriched_at = datetime.now().isoformat(timespec="seconds")
    new_body = splice(body, replacements)
    new_markdown = _with_front_matter(
        record,
        new_body,
        extraction_tool,
        has_math=has_math,
        extra_fields={
            FRONT_MATTER_TOOL_KEY: f"{OCR_TOOL} ({settings.model})",
            FRONT_MATTER_AT_KEY: enriched_at,
        },
    )

    try:
        _commit(
            attachment_key,
            record=record,
            source_path=source_path,
            source_bytes=original_bytes,
            target_path=enriched_path,
            new_markdown=new_markdown,
            extraction_tool=extraction_tool,
            has_math=has_math,
            index_root=index_root,
            lock_root=lock_root,
        )
    except PipelineLockedError as exc:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(markdown_path),
            enriched_markdown_path=str(enriched_path),
            images_dir=str(images_dir),
            error=str(exc),
        )
    except StaleMarkdownError as exc:
        return ImageOcrResult(
            ok=False,
            attachment_key=attachment_key,
            markdown_path=str(markdown_path),
            enriched_markdown_path=str(enriched_path),
            images_dir=str(images_dir),
            error=str(exc),
        )

    return ImageOcrResult(
        ok=True,
        attachment_key=attachment_key,
        markdown_path=str(markdown_path),
        enriched_markdown_path=str(enriched_path),
        images_dir=str(images_dir),
        total_refs=len(plans),
        missing_pngs=missing,
        counts=counts,
        ocr_succeeded=succeeded,
        ocr_failed=failed,
        enriched_at=enriched_at,
    )


def _run_ocr(
    plans: list[CropPlan], *, settings: ImageOcrSettings, images_dir: Path
) -> list[CropPlan]:
    """OCR each classified crop, reusing any cached result for identical (crop, prompt) pairs."""
    cache_path = images_dir / CACHE_FILENAME
    cache = _load_cache(cache_path)
    updated: list[CropPlan] = []
    dirty = False

    for plan in plans:
        prompt = TASK_PROMPTS.get(plan.crop_class)
        if prompt is None or plan.ref.png_path is None:
            updated.append(plan)
            continue
        key = _cache_key(plan.ref.png_path, prompt, settings.model)
        if key in cache:
            updated.append(CropPlan(plan.ref, plan.crop_class, ocr_text=cache[key]))
            continue
        try:
            raw = generate(
                settings.base_url,
                settings.model,
                prompt,
                plan.ref.png_path,
                timeout=settings.per_image_timeout_seconds,
            )
        except OllamaError as exc:
            updated.append(CropPlan(plan.ref, plan.crop_class, error=str(exc)))
            continue
        text = sanitize_ocr_output(raw)
        cache[key] = text
        dirty = True
        updated.append(CropPlan(plan.ref, plan.crop_class, ocr_text=text))

    if dirty:
        _save_cache(cache_path, cache)
    return updated


def _cache_key(png_path: Path, prompt: str, model: str) -> str:
    """Key cache entries by everything that determines the output: crop *content*, the task
    prompt, the serving model, and a schema version. A regenerated crop, a different model, or a
    changed sanitizer all miss correctly instead of serving stale output."""
    digest = hashlib.sha256()
    digest.update(png_path.read_bytes())
    digest.update(b"\x00")
    digest.update(prompt.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(CACHE_SCHEMA_VERSION.encode("utf-8"))
    return digest.hexdigest()


def _load_cache(cache_path: Path) -> dict[str, str]:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, str)}


def _save_cache(cache_path: Path, cache: dict[str, str]) -> None:
    # Best effort: losing the cache costs time on a resume, never correctness.
    with contextlib.suppress(OSError):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _commit(
    attachment_key: str,
    *,
    record: dict[str, object],
    source_path: Path,
    source_bytes: bytes,
    target_path: Path,
    new_markdown: str,
    extraction_tool: str,
    has_math: bool,
    index_root: Path,
    lock_root: Path,
) -> None:
    """Write the enriched Markdown to ``target_path`` and publish a matching index generation.

    The enriched content is written to ``target_path`` (a suffixed sibling by default, so the
    original at ``source_path`` is never modified) and the index is repointed there. When the
    suffix is empty the two paths coincide and this overwrites in place, restoring the original
    on a publication failure.

    The lock is held only around these writes, never around the OCR phase -- that can run for a
    long time on a large document, and holding a pipeline-wide write lock across it would block
    every other command.
    """
    staged_path = target_path.with_name(f".{target_path.name}.image-ocr-{os.getpid()}")
    with pipeline_write_lock(lock_root, command="ocr-images"):
        # Re-resolve under the lock: another writer may have published a generation while OCR ran.
        jsonl_path = current_generation_jsonl(index_root)
        if jsonl_path is None or attachment_key not in load_indexed_keys(jsonl_path):
            raise StaleMarkdownError(
                f"No text-sidecar record found for attachment key {attachment_key} at commit time."
            )
        # The splice was built from the original read before OCR started, possibly a long time
        # ago. If the original changed since (a re-conversion, a sync from another machine), the
        # enrichment is derived from stale content and must not be published.
        if source_path.read_bytes() != source_bytes:
            raise StaleMarkdownError(
                f"{source_path} changed while image OCR was running, so the enriched text is "
                "based on a stale copy. Nothing was written; re-run to pick up the new content."
            )

        # Snapshot whatever the target held so a publication failure can undo the write. The
        # source is never modified, so nothing about it needs restoring.
        prior_target = target_path.read_bytes() if target_path.exists() else None
        try:
            staged_path.write_text(new_markdown, encoding="utf-8", newline="\n")
            replace_with_retry(staged_path, target_path)
        except Exception:
            with contextlib.suppress(OSError):
                staged_path.unlink(missing_ok=True)
            raise

        try:
            new_text = strip_front_matter(new_markdown)
            new_record = TextIndexRecord(
                zotero_parent_key=record["zotero_parent_key"],
                zotero_attachment_key=record["zotero_attachment_key"],
                title=record["title"],
                creators=record["creators"],
                year=record["year"],
                doi=record["doi"],
                citation_key=record["citation_key"],
                source_path=record["source_path"],
                # The index now points at the enriched file, so search returns the recovered
                # equations while the original stays on disk untouched.
                markdown_path=str(target_path),
                # Computed after the write: the published generation must describe the file that
                # is now on disk, or the source-locator invariant breaks.
                markdown_sha256=_sha256(target_path),
                # Decided by the caller from whether the mathematics was actually recovered;
                # composed once so the front matter and the index record never disagree.
                extraction_tool=extraction_tool,
                char_count=len(new_text),
                word_count=len(new_text.split()),
                page_count=record["page_count"],
                classification=record["classification"],
                identity_status=record["identity_status"],
                identity_rule=record["identity_rule"],
                # Preserved, not recomputed: has_math gates the lossy-math warning, and the
                # document still contains the mathematics it did before enrichment.
                has_math=has_math,
                text=new_text,
            )
            stage_and_publish(
                index_root,
                write_jsonl_upserting_record(jsonl_path, attachment_key, new_record),
                command="ocr-images",
            )
        except Exception:
            # A failed publication must not leave an enriched file the index doesn't describe:
            # undo the write (restoring a prior enriched copy, or removing a newly created one).
            if prior_target is None:
                with contextlib.suppress(OSError):
                    target_path.unlink(missing_ok=True)
            else:
                _restore_bytes(target_path, prior_target)
            raise


def _restore_bytes(path: Path, content: bytes) -> None:
    """Atomically restore a file while the caller holds the pipeline write lock."""
    tmp_path = path.with_name(f".{path.name}.rollback-{os.getpid()}")
    try:
        tmp_path.write_bytes(content)
        replace_with_retry(tmp_path, path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
