"""Validate image-OCR classification against a synthetic LaTeX corpus with known ground truth.

Unlike the rest of the suite -- which writes ``b"%PDF"`` and mocks the extractor -- these tests
run the real conversion path over a real PDF and inspect the crops it actually produces. The
corpus is generated from ``tests/fixtures/ocr_corpus/corpus.tex`` and its PDF is committed, so no
LaTeX toolchain is needed here; see ``tools/build_ocr_corpus.py`` to regenerate it.

Nothing in this file needs a GPU, a model, or a network. The live recognition tier is opt-in and
skipped unless ZOTERO_PDF_TEXT_LIVE_OCR is set.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.image_ocr import (
    CLASS_SKIP,
    EQUATION_MIN_ASPECT,
    FIGURE_ASPECT_RANGE,
    PICTURE_TEXT_MARKER,
    TASK_PROMPTS,
    classify_crop,
    find_crop_refs,
)

CORPUS_DIR = Path(__file__).parent / "fixtures" / "ocr_corpus"
CORPUS_PDF = CORPUS_DIR / "corpus.pdf"
EXPECTED = json.loads((CORPUS_DIR / "expected.json").read_text(encoding="utf-8"))["elements"]
MARKER_RE = re.compile(r"CORPUSMARK-[A-Z]+-\d+")


def _convert_corpus():
    """Run the real extractor over the corpus PDF and return (marker -> CropRef, body)."""
    import pymupdf4llm

    tmp = tempfile.TemporaryDirectory()
    images_dir = Path(tmp.name) / "images"
    images_dir.mkdir()
    body = pymupdf4llm.to_markdown(
        str(CORPUS_PDF),
        write_images=True,
        image_path=str(images_dir),
        image_format="png",
        image_size_limit=0.05,
        dpi=150,
    )
    refs = find_crop_refs(body, images_dir)
    by_marker = {}
    for ref in refs:
        preceding = [m for m in MARKER_RE.finditer(body) if m.start() < ref.span[0]]
        if preceding:
            by_marker[preceding[-1].group(0)] = ref
    return by_marker, body, tmp


class CorpusExtractionTests(unittest.TestCase):
    """Guards the fixture and the extraction path, independent of any classification rule."""

    @classmethod
    def setUpClass(cls):
        cls.by_marker, cls.body, cls._tmp = _convert_corpus()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_element_matches_its_recorded_crop_expectation(self):
        for marker, spec in EXPECTED.items():
            with self.subTest(marker=marker):
                produced = marker in self.by_marker
                if spec["expected_class"] == "no_crop":
                    self.assertFalse(produced, f"{marker} unexpectedly produced a crop: {spec['note']}")
                else:
                    self.assertTrue(produced, f"{marker} produced no crop: {spec['note']}")

    def test_every_produced_crop_resolves_on_disk(self):
        for marker, ref in self.by_marker.items():
            with self.subTest(marker=marker):
                self.assertTrue(ref.exists, f"{marker} crop did not resolve")
                self.assertGreater(ref.width, 0)
                self.assertGreater(ref.height, 0)

    def test_geometry_alone_cannot_separate_content_from_decoration(self):
        """The corpus must keep containing counterexamples in both directions.

        This is the reason classify_crop consults more than crop geometry, so it is asserted
        rather than left as a comment: a future edit that made the corpus geometrically tidy
        would quietly remove the only evidence that the extra signals are needed.
        """
        low, high = FIGURE_ASPECT_RANGE

        # A negative sitting inside the equation band: aspect alone would send it to the model.
        decoration_in_equation_band = [
            marker
            for marker, ref in self.by_marker.items()
            if EXPECTED[marker]["expected_class"] == CLASS_SKIP and ref.aspect > EQUATION_MIN_ASPECT
        ]
        self.assertTrue(
            decoration_in_equation_band,
            "corpus no longer contains decoration shaped like a display equation",
        )

        # A positive sitting in the gap between the landmark bands, where no band claims it.
        def _in_gap(ref):
            return high < ref.aspect <= EQUATION_MIN_ASPECT or low > ref.aspect >= 0.1

        content_in_gap = [
            marker
            for marker, ref in self.by_marker.items()
            if EXPECTED[marker]["expected_class"] in TASK_PROMPTS and _in_gap(ref)
        ]
        self.assertTrue(
            content_in_gap,
            "corpus no longer contains real content outside the landmark aspect bands",
        )

    def test_the_figure_carries_both_of_its_textual_signals(self):
        # FIG-002 is a pgfplots chart: its axis labels are text, so conversion attaches a
        # picture-text marker, and it carries a "Fig. 2" caption -- both signals classify_crop
        # leans on. (FIG-001 is pure vector with no text, so it has no marker and is recognised
        # as a figure by its caption alone; that path is covered by the classification test.)
        figure = self.by_marker["CORPUSMARK-FIG-002"]
        neighbours = f"{figure.text_before}\n{figure.text_after}"
        self.assertIn(PICTURE_TEXT_MARKER, neighbours)
        self.assertIn("Fig. 2", self.body)

    def test_equations_carry_no_caption_or_picture_marker(self):
        """What separates the gap-band equation from the gap-band figure is its surroundings."""
        equation = self.by_marker["CORPUSMARK-EQ-009"]
        neighbours = f"{equation.text_before}\n{equation.text_after}"
        self.assertNotIn(PICTURE_TEXT_MARKER, neighbours)


class CorpusClassificationTests(unittest.TestCase):
    """Scores classify_crop against the ground truth. Skips until a rule is implemented."""

    @classmethod
    def setUpClass(cls):
        cls.by_marker, cls.body, cls._tmp = _convert_corpus()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_classification_matches_ground_truth(self):
        """The deterministic heuristic must match ground truth on every crop except the ones
        explicitly documented as blind spots -- decorations that geometry and compression cannot
        catch, which are the reason the VLM classifier exists. Those are pinned to the wrong answer
        the heuristic actually gives, so this guards the boundary in both directions: a heuristic
        regression on a normal crop fails, and a heuristic *improvement* on a blind spot also fails
        (telling you to drop the flag)."""
        mismatches = []
        recovered = []
        for marker, ref in sorted(self.by_marker.items()):
            spec = EXPECTED[marker]
            expected = spec["expected_class"]
            blind_spot = spec.get("heuristic_blind_spot")
            try:
                actual = classify_crop(ref, has_math=True)
            except NotImplementedError:
                self.skipTest(
                    "classify_crop() is not implemented yet; this test scores it against the "
                    "corpus ground truth as soon as it is."
                )
            geom = f"({ref.width}x{ref.height}, aspect {ref.aspect:.2f})"
            if blind_spot:
                if actual == expected:
                    recovered.append(f"  {marker}: heuristic now returns {expected!r} {geom}; "
                                     f"drop its heuristic_blind_spot flag in expected.json")
                elif actual != blind_spot:
                    mismatches.append(f"  {marker}: documented blind spot expected {blind_spot!r}, "
                                      f"got {actual!r} {geom}")
            elif actual != expected:
                mismatches.append(
                    f"  {marker}: expected {expected!r}, got {actual!r} {geom} -- {spec['note']}"
                )
        self.assertFalse(mismatches, "classification disagreed with ground truth:\n" + "\n".join(mismatches))
        self.assertFalse(recovered, "heuristic improved past a documented blind spot:\n" + "\n".join(recovered))


@unittest.skipUnless(
    os.environ.get("ZOTERO_PDF_TEXT_LIVE_OCR"),
    "set ZOTERO_PDF_TEXT_LIVE_OCR=1 with Ollama running to score real recognition output",
)
class CorpusRecognitionTests(unittest.TestCase):
    """Opt-in: scores what the OCR model actually returns for each known element.

    Loose token matching rather than exact LaTeX comparison -- there are many correct ways to
    write the same expression, and asserting one of them would fail on a better answer.
    """

    @classmethod
    def setUpClass(cls):
        cls.by_marker, cls.body, cls._tmp = _convert_corpus()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_recognised_text_contains_the_expected_notation(self):
        from zotero_pdf_text._ollama_client import generate, probe
        from zotero_pdf_text.config import ImageOcrSettings

        settings = ImageOcrSettings()
        status = probe(settings.base_url, settings.model)
        if not status.ok:
            self.skipTest(status.detail)

        for marker, spec in sorted(EXPECTED.items()):
            tokens = spec.get("expected_tokens") or []
            if not tokens or marker not in self.by_marker:
                continue
            with self.subTest(marker=marker):
                text = generate(
                    settings.base_url,
                    settings.model,
                    TASK_PROMPTS[spec["expected_class"]],
                    self.by_marker[marker].png_path,
                    timeout=settings.per_image_timeout_seconds,
                )
                lowered = text.lower()
                self.assertTrue(
                    any(token.lower() in lowered for token in tokens),
                    f"{marker}: none of {tokens} appeared in {text!r}",
                )


if __name__ == "__main__":
    unittest.main()
