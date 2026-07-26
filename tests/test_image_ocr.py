import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zotero_pdf_text.config import ImageOcrSettings
from zotero_pdf_text.image_ocr import (
    CAPTION_FIGURE_RE,
    CAPTION_MAX_ASPECT,
    CAPTION_TABLE_RE,
    CLASS_FIGURE,
    CLASS_FORMULA,
    CLASS_SKIP,
    CLASS_TABLE,
    PICTURE_TEXT_MARKER,
    TASK_PROMPTS,
    CropPlan,
    _cache_key,
    _run_ocr,
    classify_crop,
    composite_extraction_tool,
    enriched_path_for,
    find_crop_refs,
    front_matter_fields,
    link_basename,
    plan_crops,
    read_png_size,
    render_replacement,
    resolve_under_output_root,
    sanitize_ocr_output,
    source_path_for,
    splice,
)


def _png_bytes(width: int, height: int) -> bytes:
    """A PNG header valid enough for read_png_size, which only parses IHDR."""
    ihdr = struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def _write_png(directory: Path, name: str, width: int, height: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(_png_bytes(width, height))
    return path


class ReadPngSizeTests(unittest.TestCase):
    def test_reads_dimensions_from_ihdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_png(Path(tmp), "crop.png", 537, 28)
            self.assertEqual(read_png_size(path), (537, 28))

    def test_rejects_non_png_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            not_png = root / "not.png"
            not_png.write_bytes(b"GIF89a" + b"\x00" * 32)
            self.assertIsNone(read_png_size(not_png))
            self.assertIsNone(read_png_size(root / "absent.png"))

    def test_rejects_zero_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_png(Path(tmp), "empty.png", 0, 10)
            self.assertIsNone(read_png_size(path))


class LinkBasenameTests(unittest.TestCase):
    def test_extracts_basename_from_either_separator(self):
        # Both must work on every platform: converted Markdown carries whichever separator the
        # converting machine used, and CI runs on Windows, macOS and Linux.
        self.assertEqual(link_basename(r"C:\Users\Someone\out\images\doc\crop-01.png"), "crop-01.png")
        self.assertEqual(link_basename("C:/Users/Someone/out/images/doc/crop-01.png"), "crop-01.png")
        self.assertEqual(link_basename("/home/someone/out/crop-01.png"), "crop-01.png")

    def test_handles_relative_bare_and_decorated_targets(self):
        self.assertEqual(link_basename("crop-01.png"), "crop-01.png")
        self.assertEqual(link_basename("<images/doc/crop-01.png>"), "crop-01.png")
        self.assertEqual(link_basename('images/doc/crop-01.png "A title"'), "crop-01.png")
        self.assertEqual(link_basename("images/doc/crop%2001.png"), "crop 01.png")


class FindCropRefsTests(unittest.TestCase):
    def test_resolves_by_basename_when_link_points_at_another_machine(self):
        # Real converted libraries carry absolute paths written by whichever machine ran the
        # conversion. Trusting them yields "no images found" -- plausible and entirely wrong.
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            _write_png(images_dir, "crop-01.png", 537, 28)
            body = "Intro\n\n![](C:/Users/SomeoneElse/Sync/out/images/doc/crop-01.png)\n\nOutro"

            refs = find_crop_refs(body, images_dir)

            self.assertEqual(len(refs), 1)
            self.assertTrue(refs[0].exists)
            self.assertEqual(refs[0].png_path, images_dir / "crop-01.png")
            self.assertEqual((refs[0].width, refs[0].height), (537, 28))

    def test_collects_the_caption_label_from_two_lines_back(self):
        """The lead line must come out of real Markdown, not merely be accepted by the constructor:
        it is the only place a caption label appears once the title sits between it and the crop."""
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            _write_png(images_dir, "crop-01.png", 860, 98)
            body = (
                "Preceding paragraph of prose.\n\n"
                "Table 1\n\n"
                "_Descriptive Statistics for the Response Formats_\n\n"
                "![](crop-01.png)\n\n"
                "_Note._ Standard deviations appear in parentheses.\n"
            )

            ref = find_crop_refs(body, images_dir)[0]

            self.assertEqual(ref.text_before, "_Descriptive Statistics for the Response Formats_")
            self.assertEqual(ref.text_lead, "Table 1")
            self.assertTrue(ref.text_after.startswith("_Note._"))

    def test_the_lead_line_is_empty_when_nothing_precedes_the_nearest_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            _write_png(images_dir, "crop-01.png", 537, 28)

            ref = find_crop_refs("Only one line\n\n![](crop-01.png)\n", images_dir)[0]

            self.assertEqual(ref.text_before, "Only one line")
            self.assertEqual(ref.text_lead, "")

    def test_resolves_backslash_links_on_every_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            _write_png(images_dir, "crop-02.png", 100, 50)
            body = r"![](C:\Users\SomeoneElse\out\images\doc\crop-02.png)"

            refs = find_crop_refs(body, images_dir)

            self.assertTrue(refs[0].exists)

    def test_missing_png_is_recorded_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            images_dir.mkdir(parents=True)
            refs = find_crop_refs("![](gone.png)", images_dir)

            self.assertEqual(len(refs), 1)
            self.assertFalse(refs[0].exists)
            self.assertEqual(refs[0].aspect, 0.0)

    def test_captures_nearest_non_blank_neighbours_skipping_other_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            _write_png(images_dir, "a.png", 10, 10)
            _write_png(images_dir, "b.png", 10, 10)
            body = "Lead in text\n\n![](a.png)\n\n![](b.png)\n\n**Fig. 3** The caption"

            refs = find_crop_refs(body, images_dir)

            self.assertEqual(refs[0].text_before, "Lead in text")
            # The neighbouring image line is skipped so the caption is still found.
            self.assertEqual(refs[0].text_after, "**Fig. 3** The caption")


class CaptionRecognitionTests(unittest.TestCase):
    def test_matches_through_markdown_emphasis(self):
        # Converted output wraps captions in emphasis: "**Fig. 3** Plots showing...". A rule
        # anchored straight at the label would miss every real caption.
        self.assertTrue(CAPTION_FIGURE_RE.search("**Fig. 3** Plots showing the location"))
        self.assertTrue(CAPTION_FIGURE_RE.search("Figure 12 -- overview"))
        self.assertTrue(CAPTION_FIGURE_RE.search("_Abb. 4_ Darstellung"))
        self.assertTrue(CAPTION_TABLE_RE.search("**Table 2** Summary statistics"))
        self.assertTrue(CAPTION_TABLE_RE.search("Tabelle 7 Ergebnisse"))

    def test_does_not_match_prose_mentioning_a_figure(self):
        self.assertIsNone(CAPTION_FIGURE_RE.search("as shown in the figure above"))
        self.assertIsNone(CAPTION_TABLE_RE.search("the table of contents"))


class SpliceTests(unittest.TestCase):
    def test_applies_replacements_back_to_front(self):
        body = "AAA[1]BBB[2]CCC"
        result = splice(body, [((3, 6), "one"), ((9, 12), "two")])
        self.assertEqual(result, "AAAoneBBBtwoCCC")

    def test_order_of_input_does_not_matter(self):
        body = "AAA[1]BBB[2]CCC"
        forward = splice(body, [((3, 6), "one"), ((9, 12), "two")])
        reverse = splice(body, [((9, 12), "two"), ((3, 6), "one")])
        self.assertEqual(forward, reverse)

    def test_replacement_longer_than_original_keeps_later_spans_valid(self):
        body = "x[1]y[2]z"
        result = splice(body, [((1, 4), "A" * 40), ((5, 8), "B")])
        self.assertEqual(result, "x" + "A" * 40 + "yBz")


class SanitizeTests(unittest.TestCase):
    def test_strips_image_syntax_from_model_output(self):
        # A model-emitted image reference points at nothing, and a later run would try to resolve
        # it against the images directory and count the miss as a missing crop.
        self.assertEqual(sanitize_ocr_output("before ![](ghost.png) after"), "before  after")

    def test_truncates_overlong_responses(self):
        cleaned = sanitize_ocr_output("x" * 100, max_chars=20)
        self.assertLess(len(cleaned), 100)
        self.assertIn("truncated", cleaned)

    def test_leaves_horizontal_rules_alone(self):
        # A bare --- in the body is inert: front-matter parsing only strips the leading block.
        self.assertEqual(sanitize_ocr_output("a\n---\nb"), "a\n---\nb")


class RenderReplacementTests(unittest.TestCase):
    def test_formula_replaces_placeholder_with_display_math(self):
        rendered = render_replacement(CLASS_FORMULA, "E = mc^2", "![](crop.png)")
        self.assertEqual(rendered, "$$\nE = mc^2\n$$")
        self.assertNotIn("crop.png", rendered)

    def test_figure_keeps_the_image_and_appends_the_description(self):
        # A plot's pixels carry information no description reproduces, so the link stays.
        rendered = render_replacement(CLASS_FIGURE, "A scatter plot.", "![](crop.png)")
        self.assertTrue(rendered.startswith("![](crop.png)"))
        self.assertIn("A scatter plot.", rendered)

    def test_table_replaces_placeholder(self):
        rendered = render_replacement(CLASS_TABLE, "| a | b |", "![](crop.png)")
        self.assertEqual(rendered, "| a | b |")

    def test_empty_ocr_text_leaves_the_placeholder_untouched(self):
        self.assertEqual(render_replacement(CLASS_FORMULA, "", "![](crop.png)"), "![](crop.png)")

    def test_skip_leaves_the_placeholder_untouched(self):
        self.assertEqual(render_replacement(CLASS_SKIP, "ignored", "![](crop.png)"), "![](crop.png)")


class FrontMatterFieldsTests(unittest.TestCase):
    def test_reads_flat_unquoted_values(self):
        markdown = '---\ntitle: "A Title"\nhas_math: true\n---\n\nbody'
        fields = front_matter_fields(markdown)
        self.assertEqual(fields["title"], "A Title")
        self.assertEqual(fields["has_math"], "true")

    def test_returns_empty_without_front_matter(self):
        self.assertEqual(front_matter_fields("# Just a heading"), {})


class CompositeExtractionToolTests(unittest.TestCase):
    def test_appends_marker_to_the_original_tool(self):
        self.assertEqual(
            composite_extraction_tool("pymupdf4llm.to_markdown"), "pymupdf4llm.to_markdown+glm-ocr"
        )

    def test_does_not_stack_on_repeated_enrichment(self):
        once = composite_extraction_tool("pymupdf4llm.to_markdown")
        self.assertEqual(composite_extraction_tool(once), once)


class EnrichedPathTests(unittest.TestCase):
    def test_enriched_sibling_carries_the_suffix_before_the_extension(self):
        source = Path("/lib/markdown/0001_paper.md")
        self.assertEqual(
            enriched_path_for(source, "_ocr_eq"), Path("/lib/markdown/0001_paper_ocr_eq.md")
        )

    def test_source_is_recovered_by_stripping_the_suffix(self):
        enriched = Path("/lib/markdown/0001_paper_ocr_eq.md")
        self.assertEqual(
            source_path_for(enriched, "_ocr_eq"), Path("/lib/markdown/0001_paper.md")
        )

    def test_source_of_a_pristine_path_is_itself(self):
        original = Path("/lib/markdown/0001_paper.md")
        self.assertEqual(source_path_for(original, "_ocr_eq"), original)

    def test_round_trips(self):
        source = Path("/lib/markdown/0001_paper.md")
        self.assertEqual(source_path_for(enriched_path_for(source, "_ocr_eq"), "_ocr_eq"), source)

    def test_empty_suffix_means_in_place(self):
        # No sibling: enrichment overwrites the source, and there is no suffix to strip back.
        source = Path("/lib/markdown/0001_paper.md")
        self.assertEqual(enriched_path_for(source, ""), source)
        self.assertEqual(source_path_for(source, ""), source)


class ResolveUnderOutputRootTests(unittest.TestCase):
    def test_returns_the_stored_path_when_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "markdown" / "doc.md"
            target.parent.mkdir(parents=True)
            target.write_text("x", encoding="utf-8")
            self.assertEqual(resolve_under_output_root(target, root), target)

    def test_reroots_a_path_recorded_by_another_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "converted_text"
            actual = output_root / "verified" / "run1" / "markdown" / "doc.md"
            actual.parent.mkdir(parents=True)
            actual.write_text("x", encoding="utf-8")
            stored = Path("C:/Users/SomeoneElse/Sync/converted_text/verified/run1/markdown/doc.md")

            self.assertEqual(resolve_under_output_root(stored, output_root), actual)

    def test_returns_none_when_nothing_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored = Path("C:/Users/SomeoneElse/out/markdown/absent.md")
            self.assertIsNone(resolve_under_output_root(stored, Path(tmp)))


class ClassificationTests(unittest.TestCase):
    def test_plan_assigns_a_real_class_to_each_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            _write_png(images_dir, "crop.png", 537, 28)

            plans = plan_crops("![](crop.png)", images_dir, has_math=True)

            self.assertEqual(len(plans), 1)
            self.assertIn(plans[0].crop_class, {CLASS_SKIP, CLASS_FORMULA, CLASS_TABLE, CLASS_FIGURE})

    def test_missing_crops_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp) / "images" / "doc"
            images_dir.mkdir(parents=True)

            plans = plan_crops("![](gone.png)", images_dir, has_math=True)

            self.assertEqual(plans[0].crop_class, CLASS_SKIP)

    def test_low_complexity_crop_is_skipped_regardless_of_aspect(self):
        # A near-solid crop shaped exactly like a wide equation must still be skipped: this is the
        # NEG-004 case that geometry alone cannot catch.
        from zotero_pdf_text.image_ocr import classify_crop
        from zotero_pdf_text.image_ocr import CropRef

        solid_bar = CropRef(
            span=(0, 0), markup="", link="", png_path=Path("x.png"),
            width=946, height=65, text_before="", text_after="", byte_size=468,
        )
        self.assertEqual(classify_crop(solid_bar, has_math=True), CLASS_SKIP)

    def test_picture_marker_forces_figure_even_in_the_gap_band(self):
        from zotero_pdf_text.image_ocr import classify_crop, PICTURE_TEXT_MARKER
        from zotero_pdf_text.image_ocr import CropRef

        gap_figure = CropRef(
            span=(0, 0), markup="", link="", png_path=Path("x.png"),
            width=357, height=210, text_before="", text_after=PICTURE_TEXT_MARKER,
            byte_size=5109,
        )
        self.assertEqual(classify_crop(gap_figure, has_math=True), CLASS_FIGURE)


class TableAndCrossReferenceTests(unittest.TestCase):
    """The table path cannot be produced by the corpus generator (pymupdf4llm turns tabulars into
    text), so it is exercised here over CropRef values with geometry taken from the real library."""

    @staticmethod
    def _ref(width, height, byte_size, *, before="", after="", lead=""):
        from zotero_pdf_text.image_ocr import CropRef

        return CropRef(
            span=(0, 0), markup="", link="", png_path=Path("x.png"),
            width=width, height=height, text_before=before, text_after=after, byte_size=byte_size,
            text_lead=lead,
        )

    def test_blocky_crop_under_a_table_caption_is_a_table(self):
        from zotero_pdf_text.image_ocr import classify_crop

        # Real geometry: a genuine table crop is blocky (aspect ~2), sitting under "TABLE 2 ...".
        genuine = self._ref(378, 170, 20000, before="TABLE 2  Descriptive statistics")
        self.assertEqual(classify_crop(genuine, has_math=True), CLASS_TABLE)

    def test_thin_equation_beside_a_table_cross_reference_stays_a_formula(self):
        from zotero_pdf_text.image_ocr import classify_crop

        # Real geometry: a thin single-line equation (aspect ~15) next to running prose that
        # merely mentions a table. The mention is a cross-reference, not a caption.
        trap = self._ref(398, 26, 3000, before="Table 4.4 shows the estimated coefficients")
        self.assertEqual(classify_crop(trap, has_math=True), CLASS_FORMULA)

    def test_thin_equation_beside_a_figure_cross_reference_stays_a_formula(self):
        from zotero_pdf_text.image_ocr import classify_crop

        trap = self._ref(365, 26, 3000, after="Figure 2 shows the relationship between the terms")
        self.assertEqual(classify_crop(trap, has_math=True), CLASS_FORMULA)

    def test_blocky_crop_under_a_figure_caption_without_a_picture_marker_is_a_figure(self):
        from zotero_pdf_text.image_ocr import classify_crop

        captioned = self._ref(340, 300, 30000, after="Fig. 3  Estimated response surface")
        self.assertEqual(classify_crop(captioned, has_math=True), CLASS_FIGURE)


class CaptionBlockTests(unittest.TestCase):
    """The caption-block rule: an italicised title line before AND a note line after.

    Both conjuncts are load-bearing, and the reason is a homograph. A note line terminates a caption
    in APA style, but a textbook uses "**_Note:_**" to open a pedagogical aside that can follow
    anything -- including a display equation. Measured across the converted library, a note-only
    version of this rule claimed two real display equations and would have replaced their rendered
    notation with LaTeX invented from the image. The shapes below are those two cases plus the
    figures the rule must still recover; geometry is taken from the real crops.
    """

    # Re-wrapped: accessing a staticmethod off its class yields a plain function, which would
    # rebind as an instance method here and swallow the first argument as self.
    _ref = staticmethod(TableAndCrossReferenceTests._ref)

    def test_a_wide_strip_inside_a_caption_block_is_a_figure(self):
        """A slider screenshot: aspect ~9.8, no picture marker, geometrically an equation. Before
        the rule this had its image link replaced by LaTeX hallucinated from a UI widget."""
        strip = self._ref(
            813, 83, 4050,
            before="_Single-Range Slider (Panel A) and Dual-Range Slider (Panel B)_",
            after="_Note._ The sliders were created with a range slider plugin.",
        )
        self.assertEqual(classify_crop(strip, has_math=True), CLASS_FIGURE)

    def test_an_equation_followed_by_a_textbook_note_stays_a_formula(self):
        """The first counterexample: prose flows into the crop, so there is no title line. A
        note-only rule sends this to the figure prompt and the notation is lost."""
        equation = self._ref(
            291, 32, 3750,
            before="3. **_Fit an ARMA (p_** , **_q) model:_** the variance is estimated by",
            after="**_Note:_** The increased variability is concerning.",
        )
        self.assertEqual(classify_crop(equation, has_math=True), CLASS_FORMULA)

    def test_an_equation_under_a_heading_and_before_a_note_stays_a_formula(self):
        """The second counterexample: the line before contains emphasis but is a heading, not a
        title. Rejected because the title pattern is anchored -- the line starts with '#', not with
        the emphasis marker -- so a rule keyed on "contains emphasis" would misroute it.
        """
        equation = self._ref(
            465, 34, 5730,
            before="###### (4) **_The final fitted model is_**",
            after="**_Note:_** The above sequence of commands illustrates the steps.",
        )
        self.assertEqual(classify_crop(equation, has_math=True), CLASS_FORMULA)

    def test_a_note_under_a_table_row_does_not_make_a_strip_a_figure(self):
        """A table note is not a figure caption. The line before is a pipe row, so the rule
        declines rather than guessing -- the crop keeps whatever the geometry decided."""
        strip = self._ref(
            354, 15, 4230,
            before="|3|3.633|.892|.059|.097||",
            after="Note. In all runs the same optimization function was used.",
        )
        self.assertNotEqual(classify_crop(strip, has_math=True), CLASS_FIGURE)

    def test_a_wide_captioned_table_reaches_the_table_prompt_not_the_figure_prompt(self):
        """The table layout is the figure layout with a different label, and a wide table skips the
        aspect-guarded table check entirely -- so without the lead line this crop would be forced
        through the figure prompt, keeping its image and gaining prose instead of its cells."""
        wide_table = self._ref(
            860, 98, 5060,
            lead="Table 1",
            before="_Descriptive Statistics for the Response Formats_",
            after="_Note._ Standard deviations appear in parentheses.",
        )
        self.assertGreater(wide_table.aspect, CAPTION_MAX_ASPECT)
        self.assertEqual(classify_crop(wide_table, has_math=True), CLASS_TABLE)

    def test_only_a_table_label_diverts_a_caption_block_from_the_figure_prompt(self):
        """The lead line can divert to the table prompt and nothing else: a figure label and an
        absent label both take the figure prior, so these cases share one assertion rather than
        pretending to test different code paths.
        """
        for lead in ("Figure 7", "", "as reported by Anders et al. in the preceding section"):
            with self.subTest(lead=lead):
                crop = self._ref(
                    860, 98, 5060,
                    lead=lead,
                    before="_Estimated Consensus Intervals for Verbal Quantifiers_",
                    after="_Note._ Black horizontal interval: the estimated consensus.",
                )
                self.assertEqual(classify_crop(crop, has_math=True), CLASS_FIGURE)

    def test_a_table_cross_reference_on_the_lead_line_is_not_a_table_label(self):
        """The lead label must *be* the line. CAPTION_TABLE_RE is anchored only at the start, so it
        also matches "Table 4 shows the coefficients" -- a running-prose cross-reference, which is
        what CAPTION_MAX_ASPECT exists to reject. This rule runs outside that guard, so accepting a
        sentence here would send a figure to the table prompt and replace its image link with cells
        invented from a plot -- the expensive direction this whole rule exists to close.
        """
        strip = self._ref(
            860, 98, 5060,
            lead="Table 2 summarizes the fitted coefficients.",
            before="_Recall the Estimator Used Throughout_",
            after="_Note._ Shaded band is the 95% interval.",
        )
        self.assertEqual(classify_crop(strip, has_math=True), CLASS_FIGURE)

    def test_prose_that_merely_opens_and_closes_with_emphasis_is_not_a_caption_title(self):
        """A line can start and end with emphasis without being a title. Combined with a bare
        "Note." line this would carry an equation into the figure prompt, where it is described
        rather than transcribed and its notation never reaches the index.
        """
        for before in ("_See_ the appendix for _details_",
                       "_x_ is defined as follows, where _y_"):
            with self.subTest(before=before):
                equation = self._ref(
                    397, 98, 2300,
                    before=before,
                    after="Note. Values are rounded to two decimals.",
                )
                self.assertEqual(classify_crop(equation, has_math=True), CLASS_FORMULA)

    def test_a_title_carrying_inline_markup_is_still_a_title(self):
        """Why "no interior underscores" is the wrong rule: real caption titles contain markup, and
        rejecting them would send the figure to the formula prompt -- destroying its image link."""
        titled = self._ref(
            860, 98, 5060,
            lead="Figure 2",
            before="_Population Relationships Among MLM R_<sup>_2_</sup> _Measures_",
            after="_Note._ Adapted from the cited framework.",
        )
        self.assertEqual(classify_crop(titled, has_math=True), CLASS_FIGURE)

    def test_a_table_labelled_block_without_a_closing_note_stays_a_formula(self):
        """The real-article tier contains a "Table 1" caption over a grid of prior distributions
        that is labelled *equation*: for search the notation is the content, not the grid holding
        it. It reaches the formula prompt only because the following line is prose rather than a
        note, so the note requirement -- not the label logic -- is what protects it. A paper closing
        the same block with a note would send notation to the table prompt, which is why loosening
        that requirement must fail here rather than silently degrade the math path.
        """
        notation_under_a_table_label = self._ref(
            807, 411, 65000,
            lead="# **Table 1**",
            before="_Default Prior Distributions for the Consensus Model_",
            after="the means for reasons of identifiability:",
        )
        self.assertEqual(classify_crop(notation_under_a_table_label, has_math=True), CLASS_FORMULA)

    def test_a_title_alone_does_not_make_a_thin_strip_a_figure(self):
        """Half a caption block is not a caption block: an italicised line can also be running
        emphasis. Without the closing note the rule must not fire."""
        strip = self._ref(
            813, 83, 4050,
            before="_Single-Range Slider (Panel A) and Dual-Range Slider (Panel B)_",
            after="where the weights are normalised to sum to one",
        )
        self.assertEqual(classify_crop(strip, has_math=True), CLASS_FORMULA)


class PictureMarkerTests(unittest.TestCase):
    def test_marker_constant_matches_what_the_extractor_emits(self):
        self.assertEqual(PICTURE_TEXT_MARKER, "<!-- Start of picture text -->")


class CacheKeyTests(unittest.TestCase):
    """The cache key must depend on the model, so switching image_ocr.model never serves another
    model's output. Guards the regression Codex flagged: a content+prompt-only key silently reused
    the previous model's answer on --force."""

    def _png(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        return _write_png(Path(self._tmp.name), "crop.png", 537, 28)

    def test_switching_the_model_changes_the_key(self):
        png = self._png()
        prompt = TASK_PROMPTS[CLASS_FORMULA]
        self.assertNotEqual(
            _cache_key(png, prompt, "glm-ocr:q8_0"), _cache_key(png, prompt, "glm-ocr:fp16")
        )

    def test_same_inputs_give_a_stable_key(self):
        png = self._png()
        prompt = TASK_PROMPTS[CLASS_FORMULA]
        self.assertEqual(
            _cache_key(png, prompt, "glm-ocr:q8_0"), _cache_key(png, prompt, "glm-ocr:q8_0")
        )

    def test_run_ocr_does_not_reuse_another_models_cached_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            images_dir = Path(tmp)
            png = _write_png(images_dir, "crop.png", 537, 28)
            ref = find_crop_refs("![](crop.png)", images_dir)[0]
            plan = CropPlan(ref, CLASS_FORMULA)

            with patch("zotero_pdf_text.image_ocr.generate", return_value="from model A") as gen:
                first = _run_ocr([plan], settings=ImageOcrSettings(model="A"), images_dir=images_dir)
            self.assertEqual(first[0].ocr_text, "from model A")
            self.assertEqual(gen.call_count, 1)

            # Same crop and prompt, different model: must re-OCR, not serve model A's cached answer.
            with patch("zotero_pdf_text.image_ocr.generate", return_value="from model B") as gen:
                second = _run_ocr([plan], settings=ImageOcrSettings(model="B"), images_dir=images_dir)
            self.assertEqual(second[0].ocr_text, "from model B")
            self.assertEqual(gen.call_count, 1)

            # Re-running model A still hits the cache (no new generate call).
            with patch("zotero_pdf_text.image_ocr.generate", return_value="unused") as gen:
                third = _run_ocr([plan], settings=ImageOcrSettings(model="A"), images_dir=images_dir)
            self.assertEqual(third[0].ocr_text, "from model A")
            self.assertEqual(gen.call_count, 0)


if __name__ == "__main__":
    unittest.main()
