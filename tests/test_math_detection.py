import unittest

from zotero_pdf_text.math_detection import _has_math_font, _unicode_math_density


class HasMathFontTests(unittest.TestCase):
    def test_detects_known_math_font_substring(self):
        has_math, signals = _has_math_font(["Arial", "CMMI10"])
        self.assertTrue(has_math)
        self.assertEqual(signals, ["cmmi"])

    def test_no_signal_for_ordinary_fonts(self):
        has_math, signals = _has_math_font(["Arial", "Helvetica", "TimesNewRomanPSMT"])
        self.assertFalse(has_math)
        self.assertEqual(signals, [])

    def test_deduplicates_and_sorts_multiple_signals(self):
        has_math, signals = _has_math_font(["CMSY10", "CMMI7", "CMSY7"])
        self.assertTrue(has_math)
        self.assertEqual(signals, ["cmmi", "cmsy"])

    def test_case_insensitive_matching(self):
        has_math, signals = _has_math_font(["STIXMath-Regular"])
        self.assertTrue(has_math)
        self.assertIn("stix", signals)

    def test_empty_font_list(self):
        has_math, signals = _has_math_font([])
        self.assertFalse(has_math)
        self.assertEqual(signals, [])


class UnicodeMathDensityTests(unittest.TestCase):
    def test_empty_string_has_zero_density(self):
        count, density = _unicode_math_density("")
        self.assertEqual(count, 0)
        self.assertEqual(density, 0.0)

    def test_plain_prose_has_near_zero_density(self):
        text = "The quick brown fox jumps over the lazy dog. " * 20
        count, density = _unicode_math_density(text)
        self.assertEqual(count, 0)
        self.assertEqual(density, 0.0)

    def test_math_dense_text_has_high_density(self):
        text = "∀x∃y∈ℝ: ∑∫∏≠≈∇"
        count, density = _unicode_math_density(text)
        self.assertGreater(count, 0)
        self.assertGreater(density, 0.5)

    def test_sparse_math_symbols_below_threshold(self):
        text = "plain prose " * 200 + "∑"
        count, density = _unicode_math_density(text)
        self.assertEqual(count, 1)
        self.assertLess(density, 0.002)


if __name__ == "__main__":
    unittest.main()
