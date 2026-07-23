"""Score the crop classifier against the real-article benchmark tier, in CI, offline.

The synthetic corpus (test_ocr_corpus.py) proves the classifier on hand-built crops with tidy
ground truth. This tier proves it on crops from real published papers -- open first-author
preprints by M. Kloft, used with the author's permission (see benchmarks/preprints/ATTRIBUTION.md).
No PDF, model or network is involved: each committed crop carries the geometry and neighbouring
Markdown lines that classify_crop reads, so the harness rebuilds a faithful CropRef and re-runs the
*current* classifier (see benchmarks/scoring.py).

Assertions are invariants, not a frozen per-crop table, so the tier can grow with harder examples
without churn:

  1. reconstruction works -- every committed crop resolves to real pixels;
  2. floor -- overall routing accuracy stays at or above a documented minimum;
  3. equation safety -- every equation is routed to the formula prompt (a misrouted equation is the
     one unrecoverable error: its notation is lost, never OCR'd);
  4. bounded failure shape -- the only mistakes are figures conservatively over-routed to the math
     prompt. A table or equation misroute, or a brand-new error shape, fails loudly.

Regenerate the tier (needs the cached PDFs) with tools/build_preprint_benchmark.py.
"""

import json
import unittest
from pathlib import Path

from zotero_pdf_text.image_ocr import CLASS_FORMULA, classify_crop

from scoring import TIER_ROOT, load_tier, one_to_one_problems, score

PREPRINT_LABELS = json.loads(
    (TIER_ROOT / "preprints" / "labels.json").read_text(encoding="utf-8")
)["crops"]

# The heuristic's measured real-world accuracy is 85% (88/103); it never misroutes an equation and
# its misses are all conservative figure->formula over-routing. The floor sits a little below the
# measured value so ordinary label growth doesn't trip it, while a genuine regression still does.
ACCURACY_FLOOR = 0.80


class PreprintClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crops = load_tier("preprints")
        cls.score = score(cls.crops, lambda ref: classify_crop(ref, has_math=True))

    def test_the_tier_is_present_and_reconstructs(self):
        # load_tier ran in strict mode (setUpClass), so geometry, PNGs and labels already
        # correspond one-to-one or it would have raised. Here we pin that the loaded set is the
        # whole labelled tier, so a regeneration that shed crops cannot pass unnoticed.
        self.assertEqual(
            len(self.crops), len(PREPRINT_LABELS),
            "loaded crop count does not match labels.json; the tier drifted",
        )
        self.assertGreaterEqual(len(self.crops), 100, "preprint tier unexpectedly small")
        unreadable = [c.key for c in self.crops if not c.ref.exists]
        self.assertFalse(unreadable, f"crops did not resolve to pixels: {unreadable}")

    def test_routing_accuracy_stays_above_the_floor(self):
        self.assertGreaterEqual(
            self.score.accuracy, ACCURACY_FLOOR,
            "real-article routing accuracy regressed:\n" + self.score.report(),
        )

    def test_every_equation_reaches_the_formula_prompt(self):
        """An equation sent to any other prompt loses its notation for good -- never acceptable."""
        self.assertEqual(
            self.score.label_recall("equation"), 1.0,
            "an equation was misrouted (its notation would be lost):\n" + self.score.report(),
        )

    def test_the_only_mistakes_are_figures_over_routed_to_formula(self):
        """Pins the heuristic's known, bounded weakness. A new error shape -- a misrouted table, a
        figure dropped to skip, an equation misroute -- is a different failure and must surface."""
        unexpected = [
            (key, exp, got) for key, exp, got in self.score.errors
            if not (exp == "figure" and got == CLASS_FORMULA)
        ]
        self.assertFalse(
            unexpected,
            "a mistake outside the documented figure->formula blind spot appeared:\n"
            + "\n".join(f"  {k}: labelled {e!r}, routed {g!r}" for k, e, g in unexpected),
        )


class CorrespondenceDriftTests(unittest.TestCase):
    """The one-to-one check is what makes 'the tier can grow' safe: any drift between geometry,
    PNGs and labels must fail loudly rather than quietly shrink what CI scores."""

    def test_identical_key_sets_have_no_problems(self):
        keys = {"a/a_00.png", "a/a_01.png"}
        self.assertEqual(one_to_one_problems(keys, keys, keys), [])

    def test_a_geometry_entry_without_a_label_is_reported(self):
        geom = {"a/a_00.png", "a/a_01.png"}
        labels = {"a/a_00.png"}
        problems = one_to_one_problems(geom, geom, labels)
        self.assertTrue(any("a/a_01.png" in p for p in problems))

    def test_a_stale_label_without_a_crop_is_reported(self):
        geom = {"a/a_00.png"}
        labels = {"a/a_00.png", "a/a_removed.png"}
        problems = one_to_one_problems(geom, geom, labels)
        self.assertTrue(any("a/a_removed.png" in p for p in problems))

    def test_a_missing_or_orphan_png_is_reported(self):
        geom = {"a/a_00.png", "a/a_01.png"}
        png = {"a/a_00.png"}  # a_01 geometry entry has no PNG on disk
        problems = one_to_one_problems(geom, png, geom)
        self.assertTrue(any("a/a_01.png" in p for p in problems))

    def test_strict_load_of_a_consistent_tier_does_not_raise(self):
        # The real tier is consistent; strict load returning the full set is the guarantee.
        self.assertEqual(len(load_tier("preprints", strict=True)), len(PREPRINT_LABELS))


if __name__ == "__main__":
    unittest.main()
