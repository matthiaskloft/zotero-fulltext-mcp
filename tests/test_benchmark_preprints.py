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

import unittest

from zotero_pdf_text.image_ocr import CLASS_FORMULA, classify_crop

from scoring import load_tier, score

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
        self.assertGreaterEqual(len(self.crops), 100, "preprint tier crops missing or unlabelled")
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


if __name__ == "__main__":
    unittest.main()
