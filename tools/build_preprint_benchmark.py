"""Build the real-article benchmark tier from open first-author preprints.

Copyright posture: the source PDFs are fetched ON FIRST USE into ``benchmarks/preprints/.cache/``
which is gitignored and NEVER committed -- nothing here redistributes a whole PDF. Only the
derived image crops (``crops/<key>/``) and the hand-reviewed ground-truth labels
(``labels.json``) are tracked, so CI runs against them with no network and no cached PDF present.

Pipeline per paper:
  1. fetch(key)   -- download osf.io/<osf_id>/download to the cache, or reuse the cached copy.
  2. extract(key) -- run the real pymupdf4llm crop extraction, emit stable-named crops plus a
                     geometry manifest, and render montage sheets into the (gitignored) cache for
                     visual labelling.

Labels are authored separately in labels.json after reviewing the montages; this tool never
writes them, so a re-extraction cannot silently overwrite reviewed ground truth.

Usage:
    python tools/build_preprint_benchmark.py                 # every paper with an osf_id
    python tools/build_preprint_benchmark.py --key interval_truth
    python tools/build_preprint_benchmark.py --no-montage    # crops only
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TIER_DIR = REPO_ROOT / "benchmarks" / "preprints"
CACHE_DIR = TIER_DIR / ".cache"           # gitignored: PDFs + review montages
CROPS_DIR = TIER_DIR / "crops"            # tracked: extracted crops + geometry
SOURCES = TIER_DIR / "sources.json"
OSF_DOWNLOAD = "https://osf.io/{osf_id}/download"

# How much of a neighbouring Markdown line the manifest keeps when a prefix will do.
#
# 40 is measured, not guessed, but it is an observed maximum rather than a proven bound -- and no
# proof is available, because not every consumer is anchored. Sweeping the converted library
# (2,101 documents, 125,154 crops) and recording how deep into a line each check has to read gives
# a floor of 30, set by PICTURE_TEXT_MARKER; the next deepest is CAPTION_FIGURE_RE at 20.
# Reclassifying every one of those crops against truncated context finds the same boundary from
# the other side: widths 16-28 change 16k+ routings, 32 and above change none up to the widest
# width swept (80).
#
# The marker is what makes 30 a floor and also what stops it being a guarantee: classify_crop tests
# it with ``PICTURE_TEXT_MARKER in before``, a plain containment check with no anchor at all
# (image_ocr.py). Its 30 characters bound the read only because pymupdf4llm emits the marker on a
# line of its own -- true for all 27,220 occurrences in the sweep, but a property of the extractor's
# output, not of the check. A marker starting at offset 11 or later would be cut in half here.
# The ten characters of headroom over 30 are aimed squarely at that, and at CAPTION_FIGURE_RE,
# whose _CAPTION_LEAD accepts arbitrary ``[\s*_#>]*`` before the label.
CONTEXT_PREFIX_CHARS = 40

# Appended to any line the projection cut. Truncation is not neutral for a check anchored at the
# line *end*: cutting a line asserts "the line stops here", a claim about the source text that the
# cut itself invented, and it can flip an end-anchored match in either direction. Measured on the
# library, the committed ``[:200]`` rule already got 229 title decisions wrong that way -- 214
# real titles it severed, and 15 stretches of prose it cut into looking like titles.
#
# The sentinel restores the distinction between "the line ended" and "we stopped copying". It only
# has to be a character outside every end-anchored pattern's accepted trailing class
# (CAPTION_TITLE_RE's ``_[.:]?$`` and CAPTION_LABEL_TAIL's ``[\s*_.:)\]]*$``), which makes a cut
# line unable to satisfy either by accident.
CONTEXT_TRUNCATED_MARK = "…"

# Ceiling on the one branch that has to keep a whole line (see _project_context). The candidate
# test -- "the line starts with an underscore" -- cannot miss a title, but on maths-heavy papers it
# also admits body prose, because inline italic variables make an ordinary sentence *begin* with an
# underscore ("_i_ = 1 _, . . . , I_ (number of respondents) to item _j_ ..."). Unbounded, that put
# a 1,381-character methods paragraph in the manifest.
#
# 200 is the smallest bound that changed no routing anywhere in the library sweep (125,154 crops);
# 150 and 100 each cost one crop. It clears every real caption title in the tier by a wide margin
# -- the longest is 119 characters -- so here it truncates only the false candidates the
# ``startswith`` test lets through. Unlike CONTEXT_PREFIX_CHARS this bound is not lossless by
# construction, only by measurement; should a genuine title ever exceed it, the effect is a lost
# figure routing rather than a wrong one, and ContextProjectionFidelityTests fails loudly on
# regeneration instead of freezing the wrong answer.
CONTEXT_TITLE_MAX_CHARS = 200


def load_papers() -> dict:
    return json.loads(SOURCES.read_text(encoding="utf-8"))["papers"]


def fetch(key: str, meta: dict) -> Path:
    """Cache the paper's PDF on first use; reuse it thereafter. Returns the cached path."""
    osf_id = meta.get("osf_id")
    if not osf_id:
        raise SystemExit(f"{key}: no osf_id in sources.json yet; cannot fetch. Fill it in first.")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pdf = CACHE_DIR / f"{key}.pdf"
    if pdf.exists() and pdf.stat().st_size > 0:
        print(f"{key}: cache hit ({pdf.stat().st_size} bytes)")
        return pdf
    url = OSF_DOWNLOAD.format(osf_id=osf_id)
    print(f"{key}: fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "zotero-fulltext-mcp benchmark builder"})
    with urllib.request.urlopen(req, timeout=120) as response:
        data = response.read()
    if not data.startswith(b"%PDF"):
        raise SystemExit(f"{key}: downloaded content is not a PDF (osf_id {osf_id} wrong?).")
    pdf.write_bytes(data)
    print(f"{key}: cached {len(data)} bytes")
    return pdf


def _cut(line: str, width: int) -> str:
    """A line prefix that admits to being one. See CONTEXT_TRUNCATED_MARK.

    The width is always passed explicitly: the two call sites use different ones for reasons worth
    reading at the call site rather than inferring from a default.

    The mark is a one-way signal, not a decodable one -- a source line that genuinely ends in an
    ellipsis stores identically to one cut here. Harmless, because both are non-matches for every
    end-anchored check, which is all the mark has to guarantee.
    """
    return line[:width] + CONTEXT_TRUNCATED_MARK if len(line) > width else line


def _project_context(before: str, after: str, lead: str) -> tuple[str, str, str]:
    """Reduce the three neighbouring lines to just what classify_crop can read from them.

    The manifest freezes the classifier's *inputs* so CI can re-run the current classifier offline
    (see extract). That only works if a stored line answers every check exactly as the real line
    would -- which a plain character cap does not, because one check is anchored at the line end.

    Consumers fall into three groups, only one of which a prefix serves outright:

      Read near the line start -- PICTURE_TEXT_MARKER, CAPTION_FIGURE_RE, CAPTION_TABLE_RE,
      CAPTION_NOTE_RE. None was observed reading past CONTEXT_PREFIX_CHARS, so a marked prefix
      stands in for the line and no prose beyond it need be kept.

      Anchored at the line end, on text_before -- is_caption_title, whose CAPTION_TITLE_RE requires
      the closing emphasis to *be* the end of the line. No prefix can answer for it, so a title
      candidate is kept whole up to CONTEXT_TITLE_MAX_CHARS. ``startswith("_")`` is a deliberate
      over-approximation: every line CAPTION_TITLE_RE can match starts with an underscore, so the
      candidate set cannot miss one, and a non-title caught by it is merely stored longer than it
      needed to be rather than misread.

      Anchored at the line end, on text_lead -- CAPTION_TABLE_LABEL_RE, whose _CAPTION_LABEL_TAIL
      ends in ``$``. This one is knowingly left to the prefix. Every label line that matched it in
      the sweep was at most 18 characters, well inside the cut, but a longer one ("> > **Tabelle
      12.**") would lose its match and take the figure prompt instead of the table prompt. That
      direction is the safe one -- the sentinel guarantees a *lost* match rather than an invented
      one, a figure keeps its image link where a table splice would replace content, and
      ContextProjectionFidelityTests fails on regeneration rather than freezing the wrong answer.
      Widening the cut would not close it either, since the anchor needs the whole line however
      long it is; only a candidate branch like text_before's would, and no observed line needs it.

    Dropping the title branch entirely would be simpler and would store no prose at all, but it
    costs real accuracy: across the library sweep it moved 55 crops from the figure prompt to the
    formula prompt, which replaces a plot's image link with LaTeX invented from it.
    """
    is_title_candidate = before.strip().startswith("_")
    kept_before = _cut(before, CONTEXT_TITLE_MAX_CHARS if is_title_candidate else CONTEXT_PREFIX_CHARS)
    return kept_before, _cut(after, CONTEXT_PREFIX_CHARS), _cut(lead, CONTEXT_PREFIX_CHARS)


def extract(key: str, pdf: Path, montage: bool) -> int:
    """Emit stable-named crops + a geometry manifest for one paper. Returns the crop count."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    import pymupdf4llm

    from zotero_pdf_text import image_ocr as io
    from zotero_pdf_text.identity import strip_front_matter

    with tempfile.TemporaryDirectory() as tmp:
        images = Path(tmp) / "images"
        images.mkdir()
        body = strip_front_matter(
            pymupdf4llm.to_markdown(
                str(pdf), write_images=True, image_path=str(images),
                image_format="png", image_size_limit=0.05, dpi=150,
            )
        )
        refs = [r for r in io.find_crop_refs(body, images) if r.exists]

        out = CROPS_DIR / key
        out.mkdir(parents=True, exist_ok=True)
        for stale in out.glob("*.png"):  # re-extraction replaces old crops deterministically
            stale.unlink()

        geometry = []
        for i, ref in enumerate(refs):
            gid = f"{key}_{i:02d}"
            (out / f"{gid}.png").write_bytes(ref.png_path.read_bytes())
            # The three neighbouring Markdown lines are classify_crop's text signals (picture-text
            # marker, caption label, caption block). Persisting them lets the CI harness rebuild a
            # faithful CropRef and re-run the *current* classifier offline -- the benchmark freezes
            # the classifier's inputs, not a stale prediction. See tests/test_benchmark_preprints.py.
            # _project_context is what keeps "faithful" true while storing as little prose as the
            # checks allow; a plain character cap does neither reliably.
            before, after, lead = _project_context(
                ref.text_before, ref.text_after, ref.text_lead
            )
            geometry.append({
                "id": gid, "width": ref.width, "height": ref.height,
                "aspect": round(ref.aspect, 3), "complexity": round(ref.complexity, 4),
                "text_before": before,
                "text_after": after,
                "text_lead": lead,
                "heuristic": io.classify_crop(ref, has_math=True),
            })
        (out / "geometry.json").write_text(json.dumps(geometry, indent=2), encoding="utf-8")
        if montage:
            _render_montage(key, out, geometry)
    print(f"{key}: extracted {len(refs)} crops -> {(CROPS_DIR / key).relative_to(REPO_ROOT)}")
    return len(refs)


def _render_montage(key: str, crops_dir: Path, geometry: list[dict]) -> None:
    """Contact sheets for visual labelling, written to the gitignored cache."""
    import fitz

    cols, per_sheet = 3, 12
    cw, img_h, cap_h, pad = 380, 150, 46, 8
    ch = img_h + cap_h
    doc = fitz.open()
    page = None
    for i, g in enumerate(geometry):
        slot = i % per_sheet
        if slot == 0:
            rows = (min(per_sheet, len(geometry) - i) + cols - 1) // cols
            page = doc.new_page(width=cols * cw + pad, height=rows * ch + pad)
        col, row = slot % cols, slot // cols
        x0, y0 = pad + col * cw, pad + row * ch
        aw, ah = cw - 2 * pad, img_h - 2 * pad
        scale = min(aw / g["width"], ah / g["height"])
        dw, dh = g["width"] * scale, g["height"] * scale
        rect = fitz.Rect(x0 + (cw - dw) / 2, y0 + (img_h - dh) / 2,
                         x0 + (cw - dw) / 2 + dw, y0 + (img_h - dh) / 2 + dh)
        page.draw_rect(fitz.Rect(x0, y0, x0 + cw - pad, y0 + ch - pad), color=(0.8, 0.8, 0.8), width=0.5)
        page.insert_image(rect, filename=str(crops_dir / f"{g['id']}.png"))
        cap = f"{g['id']}  {g['width']}x{g['height']} ar={g['aspect']} cx={g['complexity']} heur={g['heuristic']}"
        page.insert_textbox(fitz.Rect(x0 + 4, y0 + img_h, x0 + cw - pad, y0 + ch), cap, fontsize=7)
    review = CACHE_DIR / "review"
    review.mkdir(parents=True, exist_ok=True)
    for pno in range(doc.page_count):
        doc[pno].get_pixmap(dpi=110).save(str(review / f"{key}_{pno + 1}.png"))
    print(f"{key}: {doc.page_count} montage sheet(s) -> {review.relative_to(REPO_ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", help="build only this paper (default: all with an osf_id)")
    parser.add_argument("--no-montage", action="store_true", help="skip the review montages")
    args = parser.parse_args(argv)

    papers = load_papers()
    keys = [args.key] if args.key else list(papers)
    total = 0
    for key in keys:
        meta = papers.get(key)
        if meta is None:
            raise SystemExit(f"unknown paper key: {key}")
        if not meta.get("osf_id"):
            print(f"{key}: skipped (osf_id not filled in yet)")
            continue
        total += extract(key, fetch(key, meta), montage=not args.no_montage)
    print(f"\n{total} crops across the requested paper(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
