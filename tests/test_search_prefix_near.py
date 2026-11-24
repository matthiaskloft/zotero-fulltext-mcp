"""Prefix terms and the near search mode (#77 D and E).

Both are additions to the query language: a query without a trailing "*" and a search_mode other
than "near" must keep returning exactly what it returned before.
"""

import json
import tempfile
import unittest
from pathlib import Path

from zotero_pdf_text.fts import (
    DEFAULT_NEAR_DISTANCE,
    MAX_NEAR_DISTANCE,
    MIN_PREFIX_CHARS,
    _compose_match_query,
    build_fts_index,
    parse_query_terms,
    search_fts,
)

from test_fts import _filter_record


def _build(tmp: str) -> Path:
    root = Path(tmp)
    filler = " ".join(f"filler{i}" for i in range(20))
    records = [
        _filter_record("P1", title="Scale study", text="A psychometric evaluation of the new scale."),
        _filter_record("P2", title="Theory note", text="Psychometrics as a discipline has a long history."),
        _filter_record("P3", title="Other", text="Psychology students were surveyed."),
        _filter_record("M1", title="Synthesis", text="We report a meta-analysis of priming effects."),
        _filter_record("M2", title="Commentary", text=f"The meta perspective {filler} and the analysis section."),
        _filter_record("M3", creators="John Doe", title="Doe synthesis", text="A meta analysis by Doe."),
    ]
    jsonl = root / "index.jsonl"
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    db = root / "index.sqlite"
    build_fts_index(jsonl, db)
    return db


def _keys(results) -> set[str]:
    return {r.zotero_attachment_key for r in results}


class PrefixTermTests(unittest.TestCase):
    def test_trailing_star_matches_every_token_with_that_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "psychometr*")), {"P1", "P2"})
            self.assertEqual(_keys(search_fts(db, "psych*")), {"P1", "P2", "P3"})
            # Without the star the same letters are one exact token, as before.
            self.assertEqual(search_fts(db, "psychometr"), [])

    def test_prefix_hits_report_body_match_and_highlight_whole_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            (hit,) = search_fts(db, "psychometr*", attachment_key="P1")
            self.assertEqual(hit.matched_fields, ["text"])
            self.assertIn("[psychometric]", hit.snippet)

    def test_prefix_combines_with_other_terms_modes_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "psychometr* scale")), {"P1"})
            self.assertEqual(_keys(search_fts(db, "psychometr* priming", search_mode="any_terms")), {"P1", "P2", "M1"})
            self.assertEqual(_keys(search_fts(db, "meta anal*", search_mode="phrase")), {"M1", "M3"})
            self.assertEqual(_keys(search_fts(db, "anal*", author="doe")), {"M3"})

    def test_a_star_inside_a_word_or_on_a_filter_stays_a_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(parse_query_terms("meta*analysis", "all_terms"), ["meta", "analysis"])
            self.assertEqual(_keys(search_fts(db, "meta*analysis")), _keys(search_fts(db, "meta analysis")))
            # Filters keep whole-word matching: "Do*" is the word "Do", which matches no creator.
            self.assertEqual(search_fts(db, None, author="Do*"), [])

    def test_short_prefixes_and_non_final_phrase_prefixes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            short = "p" * (MIN_PREFIX_CHARS - 1) + "*"
            for query, mode in ((short, "all_terms"), ("meta* analysis", "phrase"), ("x" * 65 + "*", "all_terms")):
                with self.subTest(query=query, mode=mode), self.assertRaises(ValueError):
                    search_fts(db, query, search_mode=mode)
            self.assertEqual(parse_query_terms("p" * MIN_PREFIX_CHARS + "*", "all_terms"), ["ppp*"])


class NearModeTests(unittest.TestCase):
    def test_near_sits_between_all_terms_and_phrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "meta analysis", search_mode="all_terms")), {"M1", "M2", "M3"})
            self.assertEqual(_keys(search_fts(db, "meta analysis", search_mode="near")), {"M1", "M3"})
            self.assertEqual(_keys(search_fts(db, "analysis meta", search_mode="near")), {"M1", "M3"})
            self.assertEqual(_keys(search_fts(db, "analysis meta", search_mode="phrase")), set())

    def test_near_distance_widens_and_narrows_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "meta analysis", search_mode="near", near_distance=30)), {"M1", "M2", "M3"})
            self.assertEqual(_keys(search_fts(db, "meta analysis", search_mode="near", near_distance=0)), {"M1", "M3"})
            self.assertEqual(_keys(search_fts(db, "meta section", search_mode="near", near_distance=0)), set())

    def test_near_works_with_prefixes_filters_and_within_one_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            self.assertEqual(_keys(search_fts(db, "meta anal*", search_mode="near")), {"M1", "M3"})
            self.assertEqual(_keys(search_fts(db, "meta analysis", search_mode="near", author="doe")), {"M3"})
            (hit,) = search_fts(db, "meta analysis", search_mode="near", attachment_key="M1")
            self.assertEqual(hit.matched_fields, ["text"])
            self.assertEqual(search_fts(db, "meta analysis", search_mode="near", attachment_key="M2"), [])

    def test_invalid_near_requests_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build(tmp)
            invalid = [
                dict(query="meta", search_mode="near"),
                dict(query="meta analysis", search_mode="all_terms", near_distance=5),
                dict(query="meta analysis", search_mode="near", near_distance=-1),
                dict(query="meta analysis", search_mode="near", near_distance=MAX_NEAR_DISTANCE + 1),
                dict(query="meta analysis", search_mode="near", near_distance=True),
                dict(query="meta analysis", search_mode="near", near_distance="5"),
            ]
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    search_fts(db, **kwargs)

    def test_match_strings_are_built_only_from_quoted_terms(self):
        self.assertEqual(
            _compose_match_query(["meta", "anal*"], "near", {}),
            f'NEAR("meta" "anal"*, {DEFAULT_NEAR_DISTANCE})',
        )
        self.assertEqual(_compose_match_query(["meta", "anal*"], "phrase", {}), '"meta anal"*')
        self.assertEqual(
            _compose_match_query(["meta", "analysis"], "near", {"author": ["doe"]}, near_distance=3),
            '(text : (NEAR("meta" "analysis", 3))) AND (creators : ("doe"))',
        )


class UnchangedBehaviourTests(unittest.TestCase):
    def test_queries_without_the_new_syntax_build_the_same_match_strings(self):
        self.assertEqual(_compose_match_query(["a", "b"], "all_terms", {}), '"a" AND "b"')
        self.assertEqual(_compose_match_query(["a", "b"], "any_terms", {}), '"a" OR "b"')
        self.assertEqual(_compose_match_query(["a", "b"], "phrase", {}), '"a b"')


if __name__ == "__main__":
    unittest.main()
