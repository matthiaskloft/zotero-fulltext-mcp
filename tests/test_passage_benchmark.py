"""Passage-level benchmark (benchmarks/passages.py, chunking.py; roadmap step S1a, issue #107).

Synthetic fixtures only: no private question set or library data is read or written.
"""

import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import chunking
import passages
import retrieval

from zotero_pdf_text.fts import build_fts_index, chunk_sha256

FIXTURES = Path(__file__).parent / "fixtures" / "retrieval"
CORPUS = FIXTURES / "synthetic_passages_corpus.json"
QUESTIONS = FIXTURES / "synthetic_passages.questions.json"
TOKENIZER = chunking.RegexTokenizer()


def _run(argv: list[str]) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        retrieval.main(argv)
    return buffer.getvalue()


def _span(start, end, text=None):
    return passages.HitSpan(start, end, chunk_sha256(text) if text is not None else "")


def _hit(key, start, end, tokens=10, extra=0, sha=""):
    return passages.PassageHit(key, "c-" + key, (passages.HitSpan(start, end, sha),), tokens, extra)


def _judgments(*spans, traps=(), qualifiers=()):
    return passages.Judgments(tuple(spans), tuple(traps), tuple(qualifiers))


def _ev(key, start, end, grade=3):
    return passages.Span("attachment_key", key, start, end, grade)


class ChunkerTests(unittest.TestCase):
    docs = chunking.load_corpus(CORPUS)

    def _chunks(self, text, spec):
        return chunking.chunk_document(text, chunking.parse_spec(spec), TOKENIZER)

    def test_chars_strategy_reproduces_the_current_production_slicing(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "c.jsonl"
            records = json.loads(CORPUS.read_text(encoding="utf-8"))
            jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            db = Path(tmp) / "index.sqlite"
            build_fts_index(jsonl, db, chunk_chars=300, overlap_chars=40)
            import sqlite3

            con = sqlite3.connect(db)
            try:
                rows = con.execute(
                    "SELECT m.zotero_attachment_key, c.start_char, c.end_char FROM chunks c "
                    "JOIN metadata m ON m.record_id = c.record_id ORDER BY m.record_id, c.chunk_index"
                ).fetchall()
            finally:
                con.close()
        expected = [
            (d.attachment_key, s, e)
            for d in self.docs.values()
            for s, e in self._chunks(d.text, "chars:300:40")
        ]
        self.assertEqual(rows, expected)

    def test_token_chunks_are_contiguous_source_slices_within_the_target(self):
        for spec in ("sentence:30:0", "sentence:30:8", "structural:30:0", "structural:30:8"):
            for doc in self.docs.values():
                for start, end in self._chunks(doc.text, spec):
                    with self.subTest(spec=spec, doc=doc.attachment_key, start=start):
                        text = doc.text[start:end]
                        self.assertEqual(text, text.strip())
                        self.assertLessEqual(TOKENIZER.count(text), 30)

    def test_zero_overlap_chunks_cover_every_character_exactly_once(self):
        for spec in ("sentence:30:0", "structural:30:0", "sentence:128:0"):
            for doc in self.docs.values():
                chunks = self._chunks(doc.text, spec)
                covered = "".join(doc.text[s:e] for s, e in chunks)
                self.assertEqual(re.sub(r"\s", "", covered), re.sub(r"\s", "", doc.text), (spec, doc.attachment_key))
                for (_, e1), (s2, _) in zip(chunks, chunks[1:], strict=False):
                    self.assertLessEqual(e1, s2)

    def test_bounded_overlap_repeats_at_most_the_overlap_budget(self):
        doc = self.docs["PATT3"]
        chunks = self._chunks(doc.text, "sentence:25:10")
        overlaps = [
            TOKENIZER.count(doc.text[s2:e1]) for (_, e1), (s2, _) in zip(chunks, chunks[1:], strict=False) if s2 < e1
        ]
        self.assertTrue(overlaps)
        self.assertLessEqual(max(overlaps), 10)
        covered = "".join(doc.text[s:e] for s, e in chunks)
        self.assertGreater(len(covered), len(doc.text.replace("\n", "")) - 100)  # grew, never lost text

    def test_structural_chunks_never_cross_a_heading(self):
        text = "# One\n\nAlpha beta gamma.\n\n## Two\n\nDelta epsilon zeta.\n\n## Three\n\nEta theta iota."
        chunks = [text[s:e] for s, e in self._chunks(text, "structural:200:0")]
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(c.startswith("#") for c in chunks))

    def test_a_table_that_fits_stays_whole_and_an_oversized_one_splits_by_rows(self):
        rows = "\n".join(f"| row{i} | {i} |" for i in range(12))
        text = f"Intro sentence here.\n\n{rows}\n\nOutro sentence here."
        whole = [text[s:e] for s, e in self._chunks(text, "structural:500:0")]
        self.assertEqual(len(whole), 1)
        split = [text[s:e] for s, e in self._chunks(text, "structural:20:0")]
        table_chunks = [c for c in split if "| row" in c]
        self.assertGreater(len(table_chunks), 1)
        row = re.compile(r"\| row\d+ \| \d+ \|")
        self.assertTrue(all(row.fullmatch(line) for c in table_chunks for line in c.splitlines() if "row" in line))
        self.assertEqual(re.sub(r"\s", "", "".join(split)), re.sub(r"\s", "", text))

    def test_abbreviations_do_not_end_a_sentence(self):
        text = "Smith et al. Reported a larger effect. See Fig. 3 for details."
        spans = [text[s:e] for s, e in chunking.sentence_spans(text)]
        self.assertEqual(spans, ["Smith et al. Reported a larger effect.", "See Fig. 3 for details."])

    def test_an_oversized_sentence_and_an_unbroken_run_are_split_without_loss(self):
        words = " ".join(f"w{i}" for i in range(100)) + "."
        text = f"{words} {'x' * 400}"
        chunks = self._chunks(text, "sentence:20:0")
        self.assertGreater(len(chunks), 5)
        self.assertEqual(re.sub(r"\s", "", "".join(text[s:e] for s, e in chunks)), re.sub(r"\s", "", text))

    def test_empty_and_whitespace_documents_yield_no_chunks(self):
        for spec in ("chars:50:0", "sentence:30:0", "structural:30:0"):
            self.assertEqual(self._chunks("", spec), [])
            self.assertEqual(self._chunks(" \n\n ", spec), [])

    def test_specs_validate_and_sweep_is_bounded(self):
        for bad in ("nope:10:0", "sentence", "sentence:x:0", "sentence:10:10", "sentence:0:0", "chars:5:-1"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                chunking.parse_spec(bad)
        specs = chunking.sweep_specs("structural")
        self.assertEqual(sorted({s.target for s in specs}), [128, 256, 384, 512, 768])
        self.assertEqual(sum(s.overlap == 0 for s in specs), 5)
        self.assertTrue(all(s.overlap <= 0.15 * s.target for s in specs))
        with self.assertRaises(SystemExit):
            chunking.sweep_specs("chars")

    def test_tokenizer_selection_and_pinned_version(self):
        tokenizer = chunking.get_tokenizer("regex")
        self.assertEqual((tokenizer.name, tokenizer.version), ("regex", "1"))
        self.assertEqual(tokenizer.count("a,b (c)"), 6)
        with self.assertRaises(SystemExit):
            chunking.get_tokenizer("mystery")


class ExperimentIndexTests(unittest.TestCase):
    docs = chunking.load_corpus(CORPUS)

    def test_build_records_distribution_and_hashes_match_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"x{chunking.EXPERIMENT_SUFFIX}"
            summary = chunking.build_experiment_index(self.docs, chunking.parse_spec("sentence:40:0"), TOKENIZER, path)
            facts = summary.to_dict()
            self.assertEqual(facts["documents"], 3)
            self.assertEqual(facts["tokenizer"], {"name": "regex", "version": "1"})
            self.assertLessEqual(facts["size_tokens"]["max"], 40)
            self.assertEqual(facts["chunks_over_target"], 0)
            self.assertGreater(facts["index_bytes"], 0)
            self.assertNotIn(tmp, json.dumps(facts))
            con = chunking.open_experiment(path)
            try:
                for row in con.execute("SELECT c.*, d.attachment_key FROM chunks c JOIN docs d USING (doc_id)"):
                    source = self.docs[row["attachment_key"]].text[row["start_char"] : row["end_char"]]
                    self.assertEqual(row["text"], source)
                    self.assertEqual(row["chunk_sha256"], hashlib.sha256(source.encode()).hexdigest())
            finally:
                con.close()

    def test_fts_uses_production_columns_weights_and_blanks_image_markup(self):
        docs = {
            "A": chunking.CorpusDoc(
                "A", "smith2020", "Body text. ![zebrafigure](/home/you/uniquepathword.png) More body.",
                "Quokka title", "Wombat Author",
            )
        }
        with tempfile.TemporaryDirectory() as tmp:
            summary = chunking.build_experiment_index(
                docs, chunking.parse_spec("sentence:40:0"), TOKENIZER, Path(tmp) / f"x{chunking.EXPERIMENT_SUFFIX}"
            )
            retriever = passages.ExperimentRetriever(summary)
            try:
                found = {q: bool(retriever.retrieve(q, "all_terms", 5)) for q in (
                    "quokka", "wombat", "smith2020", "zebrafigure", "uniquepathword", "png")}
            finally:
                retriever.close()
        self.assertEqual(
            found,
            {"quokka": True, "wombat": True, "smith2020": True, "zebrafigure": True,
             "uniquepathword": False, "png": False},
        )

    def test_a_directory_with_a_hash_or_percent_sign_is_opened_read_only_at_the_right_path(self):
        spec = chunking.parse_spec("sentence:40:0")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("scratch#1", "a%41#x"):
                path = Path(tmp) / name / f"a{chunking.EXPERIMENT_SUFFIX}"
                summary = chunking.build_experiment_index(self.docs, spec, TOKENIZER, path)
                chunking.build_experiment_index(self.docs, spec, TOKENIZER, path)  # marker check reopens it
                con = chunking.open_experiment(summary.path)
                try:
                    self.assertGreater(con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
                    with self.assertRaises(Exception):
                        con.execute("CREATE TABLE x (y)")
                finally:
                    con.close()
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["a%41#x", "scratch#1"])

    def test_refuses_to_build_inside_a_production_output_root(self):
        spec = chunking.parse_spec("sentence:40:0")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            (root / "index").mkdir(parents=True)
            (root / "index" / "current.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(SystemExit):
                chunking.build_experiment_index(self.docs, spec, TOKENIZER, root / "index" / f"a{chunking.EXPERIMENT_SUFFIX}")
            generations = Path(tmp) / "generations" / "g1"
            with self.assertRaises(SystemExit):
                chunking.build_experiment_index(self.docs, spec, TOKENIZER, generations / f"a{chunking.EXPERIMENT_SUFFIX}")

    def test_never_replaces_a_file_that_is_not_an_experimental_index(self):
        spec = chunking.parse_spec("sentence:40:0")
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / f"a{chunking.EXPERIMENT_SUFFIX}"
            existing.write_bytes(b"not an experiment")
            with self.assertRaises(SystemExit):
                chunking.build_experiment_index(self.docs, spec, TOKENIZER, existing)
            self.assertEqual(existing.read_bytes(), b"not an experiment")
            with self.assertRaises(SystemExit):
                chunking.build_experiment_index(self.docs, spec, TOKENIZER, Path(tmp) / "index.sqlite")
            fresh = Path(tmp) / f"b{chunking.EXPERIMENT_SUFFIX}"
            chunking.build_experiment_index(self.docs, spec, TOKENIZER, fresh)
            chunking.build_experiment_index(self.docs, spec, TOKENIZER, fresh)  # own artifact: replaced

    def test_neighbour_expansion_charges_the_extra_tokens_and_keeps_every_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = chunking.build_experiment_index(
                self.docs, chunking.parse_spec("sentence:30:0"), TOKENIZER, Path(tmp) / f"x{chunking.EXPERIMENT_SUFFIX}"
            )
            plain = passages.ExperimentRetriever(summary)
            wide = passages.ExperimentRetriever(summary, expand=1)
            try:
                (base, *_) = plain.retrieve("DIAG-7731-Q", "phrase", 5)
                (expanded, *_) = wide.retrieve("DIAG-7731-Q", "phrase", 5)
            finally:
                plain.close()
                wide.close()
        self.assertEqual(wide.name, "sentence:30:0+expand1")
        self.assertEqual(expanded.tokens, base.tokens)
        self.assertGreater(expanded.extra_tokens, 0)
        self.assertGreater(len(expanded.spans), 1)
        self.assertEqual(passages.hit_locator_counts(expanded, self.docs), (len(expanded.spans), len(expanded.spans)))


class MetricTests(unittest.TestCase):
    def test_ndcg_credits_each_evidence_span_once_and_discounts_by_rank(self):
        judgments = _judgments(_ev("A", 0, 10, 3), _ev("A", 100, 110, 1))
        hits = [_hit("A", 0, 20), _hit("A", 5, 15), _hit("A", 100, 120)]  # 2nd repeats the first span
        m = passages.passage_metrics(hits, judgments, None, ideal_n=3)
        dcg = 7 / 1 + 0 + 1 / 2  # grade 3 at rank 1, duplicate earns nothing, grade 1 at rank 3
        idcg = 7 / 1 + 1 / 1.5849625007211562
        self.assertAlmostEqual(m.ndcg, dcg / idcg, places=6)
        self.assertEqual(m.span_recall, 1.0)

    def test_ndcg_ideal_does_not_shrink_with_the_number_of_hits_returned(self):
        judgments = _judgments(_ev("A", 0, 30), _ev("A", 100, 130), _ev("A", 200, 230))
        one_big = [_hit("A", 0, 300)]  # covers all three spans but credits only one
        three_small = [_hit("A", 0, 30), _hit("A", 100, 130), _hit("A", 200, 230)]
        big = passages.passage_metrics(one_big, judgments, None)
        small = passages.passage_metrics(three_small, judgments, None)
        self.assertEqual(big.span_recall, small.span_recall)
        self.assertLess(big.ndcg, small.ndcg)
        self.assertAlmostEqual(small.ndcg, 1.0)
        self.assertLessEqual(big.ndcg, 1.0)

    def test_repeated_overlapping_or_expanded_context_earns_no_new_credit(self):
        judgments = _judgments(_ev("A", 0, 30), _ev("A", 100, 130))
        both = _hit("A", 0, 130)
        single = passages.passage_metrics([both], judgments, None, ideal_n=2).ndcg
        self.assertAlmostEqual(single, 1 / (1 + 1 / 1.5849625007211562))
        for hits in (
            [both, both],  # duplicate
            [both, _hit("A", 50, 120)],  # overlapping chunk touching the second span
            [both, _hit("A", 90, 140)],
        ):
            self.assertAlmostEqual(passages.passage_metrics(hits, judgments, None, ideal_n=2).ndcg, single)
        expanded = passages.PassageHit("A", "c-A", (passages.HitSpan(0, 30), passages.HitSpan(100, 130)), 10, 10)
        self.assertAlmostEqual(passages.passage_metrics([expanded, _hit("A", 100, 130)], judgments, None, ideal_n=2).ndcg, single)
        # a genuinely new span in a later hit still counts
        self.assertGreater(passages.passage_metrics([_hit("A", 0, 30), _hit("A", 100, 130)], judgments, None, ideal_n=2).ndcg, single)

    def test_a_miss_and_an_empty_list_score_zero(self):
        judgments = _judgments(_ev("A", 0, 10))
        for hits in ([], [_hit("B", 0, 10), _hit("A", 50, 60)]):
            m = passages.passage_metrics(hits, judgments, None)
            self.assertEqual((m.ndcg, m.span_recall, m.precision), (0.0, 0.0, 0.0))

    def test_span_recall_needs_half_the_span_returned(self):
        judgments = _judgments(_ev("A", 0, 100))
        self.assertEqual(passages.passage_metrics([_hit("A", 0, 49)], judgments, None).span_recall, 0.0)
        self.assertEqual(passages.passage_metrics([_hit("A", 0, 50)], judgments, None).span_recall, 1.0)
        self.assertAlmostEqual(passages.passage_metrics([_hit("A", 0, 25)], judgments, None).char_recall, 0.25)

    def test_precision_and_duplicate_rate_are_character_based(self):
        judgments = _judgments(_ev("A", 0, 50))
        hits = [_hit("A", 0, 100), _hit("A", 50, 150)]  # 200 returned, 150 unique, 50 relevant
        m = passages.passage_metrics(hits, judgments, None)
        self.assertAlmostEqual(m.precision, 50 / 200)
        self.assertAlmostEqual(m.dup_rate, 1 - 150 / 200)

    def test_citation_key_judgments_match_any_attachment_of_the_paper(self):
        judgments = _judgments(passages.Span("citation_key", "c-A", 0, 10, 3))
        self.assertEqual(passages.passage_metrics([_hit("A", 0, 10)], judgments, None).span_recall, 1.0)
        self.assertEqual(passages.passage_metrics([_hit("B", 0, 10)], judgments, None).span_recall, 0.0)

    def test_traps_and_qualifiers(self):
        judgments = _judgments(
            _ev("A", 0, 10), traps=[_ev("A", 500, 600)], qualifiers=[_ev("A", 20, 30), _ev("A", 40, 50)]
        )
        m = passages.passage_metrics([_hit("A", 0, 30), _hit("A", 550, 560)], judgments, None)
        self.assertEqual(m.trap_rate, 0.5)
        self.assertEqual(m.qualifier_recall, 0.5)
        self.assertIsNone(passages.passage_metrics([_hit("A", 0, 30)], _judgments(_ev("A", 0, 10)), None).trap_rate)

    def test_locator_validity_checks_range_and_hash_against_the_record_text(self):
        docs = {"A": chunking.CorpusDoc("A", "c-A", "0123456789" * 10)}
        good = passages.PassageHit("A", "c-A", (_span(10, 20, docs["A"].text[10:20]),), 5)
        wrong_hash = passages.PassageHit("A", "c-A", (_span(10, 20, "other"),), 5)
        out_of_range = passages.PassageHit("A", "c-A", (_span(90, 120),), 5)
        unknown_doc = passages.PassageHit("Z", "c-Z", (_span(0, 5),), 5)
        self.assertEqual(passages.hit_locator_counts(good, docs), (1, 1))
        self.assertEqual(passages.hit_locator_counts(wrong_hash, docs), (0, 1))
        self.assertEqual(passages.hit_locator_counts(out_of_range, docs), (0, 1))
        self.assertEqual(passages.hit_locator_counts(unknown_doc, docs), (0, 0))
        judgments = _judgments(_ev("A", 10, 20))
        m = passages.passage_metrics([good, wrong_hash], judgments, docs)
        self.assertEqual((m.locator_valid, m.locator_checked), (1, 2))
        self.assertEqual(m.cited_span_recall, 1.0)  # the valid hit alone still cites the span
        only_bad = passages.passage_metrics([wrong_hash], judgments, docs)
        self.assertEqual((only_bad.span_recall, only_bad.cited_span_recall), (1.0, 0.0))

    def test_budget_prefix_charges_expansion_tokens_and_never_truncates(self):
        hits = [_hit("A", 0, 10, tokens=40, extra=30), _hit("A", 20, 30, tokens=20), _hit("A", 40, 50, tokens=5)]
        self.assertEqual(passages.budget_prefix(hits, 69), [])
        self.assertEqual(len(passages.budget_prefix(hits, 70)), 1)
        self.assertEqual(len(passages.budget_prefix(hits, 90)), 2)
        # stops at the first hit that does not fit even though a later, smaller one would
        self.assertEqual(len(passages.budget_prefix([hits[0], hits[1], hits[2]], 80)), 1)
        self.assertEqual(passages.passage_metrics(hits[:1], _judgments(_ev("A", 0, 10)), None).tokens, 70)


class FusionAndChannelTests(unittest.TestCase):
    def test_rrf_scores_by_reciprocal_rank_and_merges_channels(self):
        a, b, c = _hit("A", 0, 1), _hit("B", 0, 1), _hit("C", 0, 1)
        lexical = [passages.PassageHit(h.attachment_key, h.citation_key, h.spans, 1, 0, ("lexical",)) for h in (a, b)]
        semantic = [passages.PassageHit(h.attachment_key, h.citation_key, h.spans, 1, 0, ("semantic",)) for h in (c, b)]
        fused = passages.rrf_fuse([lexical, semantic], k=60)
        self.assertEqual([h.attachment_key for h in fused], ["B", "A", "C"])  # B twice; A beats C by first-seen order
        self.assertEqual(fused[0].channels, ("lexical", "semantic"))

    def test_semantic_is_unavailable_and_so_is_hybrid_built_on_it(self):
        semantic = passages.SemanticRetriever()
        self.assertIn("no embedding backend", semantic.unavailable_reason())
        with self.assertRaises(RuntimeError):
            semantic.retrieve("x", "all_terms", 5)

        class Stub:
            name, channel = "stub", "lexical"

            def unavailable_reason(self):
                return None

            def retrieve(self, query, search_mode, limit):
                return [_hit("A", 0, 5), _hit("B", 0, 5)]

        self.assertIsNotNone(passages.HybridRetriever(Stub(), semantic).unavailable_reason())

        class StubSemantic(Stub):
            name, channel = "sem", "semantic"

            def retrieve(self, query, search_mode, limit):
                return [_hit("B", 0, 5)]

        hybrid = passages.HybridRetriever(Stub(), StubSemantic())
        self.assertIsNone(hybrid.unavailable_reason())
        self.assertEqual([h.attachment_key for h in hybrid.retrieve("q", "all_terms", 5)], ["B", "A"])


class UncertaintyTests(unittest.TestCase):
    def test_paired_bootstrap_is_deterministic_and_brackets_a_clear_effect(self):
        diffs = [0.2, 0.3, 0.25, 0.1, 0.4, 0.35, 0.15, 0.3]
        first, second = passages.paired_bootstrap(diffs), passages.paired_bootstrap(diffs)
        self.assertEqual(first, second)
        low, high = first["ci95"]
        self.assertGreater(low, 0)
        self.assertLessEqual(low, first["mean_diff"])
        self.assertGreaterEqual(high, first["mean_diff"])

    def test_zero_difference_spans_zero_and_tiny_samples_have_no_interval(self):
        low, high = passages.paired_bootstrap([0.1, -0.1, 0.2, -0.2, 0.0, 0.05, -0.05, 0.0])["ci95"]
        self.assertLess(low, 0)
        self.assertGreater(high, 0)
        self.assertIsNone(passages.paired_bootstrap([0.3])["ci95"])
        self.assertEqual(passages.paired_bootstrap([])["n"], 0)


class QuestionFileTests(unittest.TestCase):
    @staticmethod
    def _load(questions):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.questions.json"
            path.write_text(
                json.dumps({"format": retrieval.FORMAT, "version": 1, "synthetic": True, "questions": questions}),
                encoding="utf-8",
            )
            return retrieval.load_questions(path)[0]

    @staticmethod
    def _q(**fields):
        base = {"id": "q1", "query": "shrinkage"}
        base.update(fields)
        return base

    def test_expected_is_derived_from_evidence_and_defaults_apply(self):
        (q,) = self._load([self._q(evidence=[{"citation_key": "a", "quote": "x"}, {"citation_key": "a", "start_char": 1, "end_char": 5}])])
        self.assertEqual(q.expected, ({"citation_key": "a"},))
        self.assertEqual((q.split, q.evidence[0].grade), ("dev", 3))
        (older,) = self._load([self._q(expected=[{"attachment_key": "A"}])])
        self.assertEqual((older.split, older.evidence), ("dev", ()))

    def test_rejections(self):
        key = {"citation_key": "a"}
        cases = {
            "bad split": self._q(split="test", expected=[key]),
            "no span key": self._q(evidence=[{"quote": "x"}]),
            "both keys": self._q(evidence=[{"citation_key": "a", "attachment_key": "A", "quote": "x"}]),
            "quote and offsets": self._q(evidence=[{**key, "quote": "x", "start_char": 0, "end_char": 3}]),
            "neither quote nor offsets": self._q(evidence=[dict(key)]),
            "empty offsets": self._q(evidence=[{**key, "start_char": 5, "end_char": 5}]),
            "bad grade": self._q(evidence=[{**key, "quote": "x", "grade": 4}]),
            "grade on a trap": self._q(expected=[key], traps=[{**key, "quote": "x", "grade": 2}]),
            "bad occurrence": self._q(evidence=[{**key, "quote": "x", "occurrence": 0}]),
            "unknown field": self._q(evidence=[{**key, "quote": "x", "note": "y"}]),
            "no judgments": self._q(),
        }
        for name, question in cases.items():
            with self.subTest(name), self.assertRaises(SystemExit):
                self._load([question])

    def test_quotes_resolve_against_the_corpus_and_ambiguity_is_an_error(self):
        docs = {"A": chunking.CorpusDoc("A", "ca", "alpha beta alpha gamma")}

        def resolve(**span):
            (q,) = self._load([self._q(evidence=[{"attachment_key": "A", **span}])])
            return passages.resolve_judgments(q, docs).evidence[0]

        self.assertEqual((resolve(quote="gamma").start, resolve(quote="gamma").end), (17, 22))
        self.assertEqual(resolve(quote="alpha", occurrence=2).start, 11)
        for bad in ({"quote": "alpha"}, {"quote": "missing"}, {"quote": "alpha", "occurrence": 3}, {"start_char": 10, "end_char": 99}):
            with self.subTest(bad), self.assertRaises(SystemExit):
                resolve(**bad)
        (q,) = self._load([self._q(evidence=[{"attachment_key": "A", "quote": "alpha beta"}])])
        with self.assertRaises(SystemExit):
            passages.resolve_judgments(q, None)  # a quote needs the corpus
        (offsets,) = self._load([self._q(evidence=[{"attachment_key": "A", "start_char": 0, "end_char": 5}])])
        self.assertEqual(passages.resolve_judgments(offsets, None).evidence[0].end, 5)


class FixtureRunTests(unittest.TestCase):
    ARGS = [
        "--questions", str(QUESTIONS), "--corpus", str(CORPUS), "--chunking", "chars:6000:500",
        "--chunking", "sentence:40:0", "--chunking", "structural:40:6", "--budget", "60,120",
        "--k", "1,3", "--json",
    ]

    @classmethod
    def setUpClass(cls):
        cls.report = json.loads(_run(cls.ARGS))
        cls.raw = _run(cls.ARGS)

    def _config(self, label, channel="lexical"):
        return next(c for c in self.report["configurations"] if c["label"] == label and c["channel"] == channel)

    def test_reports_every_metric_for_every_series_with_setup_facts(self):
        self.assertEqual(self.report["k"], [1, 3])
        self.assertEqual(self.report["budgets"], [60, 120])
        self.assertEqual(self.report["tokenizer"], {"name": "regex", "version": "1"})
        self.assertEqual(self.report["splits"], ["dev", "heldout"])
        config = self._config("sentence:40:0")
        self.assertEqual(config["build"]["chunking"], {"strategy": "sentence", "target": 40, "overlap": 0, "unit": "tokens"})
        for key in ("chunks", "build_seconds", "index_bytes", "size_tokens", "corpus_sha256"):
            self.assertIn(key, config["build"])
        self.assertEqual(sorted(config["latency_ms"]), ["p50", "p95"])
        self.assertEqual(config["judged_questions"], 8)
        for series, values in (("topk", ["1", "3"]), ("budget", ["60", "120"])):
            self.assertEqual(sorted(config["passage"][series], key=int), values)
        agg = config["passage"]["topk"]["3"]
        for key in ("ndcg", "span_recall", "precision", "dup_rate", "locator_validity", "tokens", "n_hits"):
            self.assertIn(key, agg)
        self.assertEqual(agg["locator_validity"], 1.0)
        self.assertEqual(sorted(config["paper"]["recall"]), ["1", "3"])
        self.assertIn("exact_identifier", config["passage"]["by_type"])

    def test_equal_budget_charges_returned_tokens_and_a_large_chunk_does_not_fit(self):
        chars = self._config("chars:6000:500")["passage"]["budget"]
        sentence = self._config("sentence:40:0")["passage"]["budget"]
        self.assertEqual(chars["60"]["n_hits"], 0.0)
        self.assertEqual(chars["60"]["span_recall"], 0.0)
        self.assertGreater(sentence["60"]["span_recall"], 0.5)
        for config in self.report["configurations"]:
            if config["status"] == "ok":
                self.assertLessEqual(config["passage"]["budget"]["120"]["tokens"], 120)

    def test_heldout_and_dev_splits_are_reported_and_selectable(self):
        by_split = self._config("sentence:40:0")["passage"]["by_split"]
        self.assertEqual(sorted(by_split), ["dev", "heldout"])
        self.assertEqual((by_split["dev"]["topk"]["3"]["n"], by_split["heldout"]["topk"]["3"]["n"]), (4, 4))
        held = json.loads(_run([*self.ARGS, "--split", "heldout"]))
        self.assertEqual(held["splits"], ["heldout"])
        self.assertEqual(held["judged_questions"], 4)

    def test_paired_changes_name_regressions_and_intervals(self):
        self.assertEqual(len(self.report["changes"]), 2)
        rows = self.report["changes"][0]["rows"]
        self.assertEqual(len(rows), 8)  # (2 cutoffs + 2 budgets) x 2 primary metrics
        row = next(r for r in rows if r["series"] == "topk:1" and r["metric"] == "ndcg")
        self.assertEqual(row["n"], 8)
        self.assertEqual(sorted(row), ["ci95", "losses", "mean_diff", "metric", "n", "regressed", "series", "wins"])
        self.assertTrue(set(row["regressed"]) <= {f"p0{i}" for i in range(1, 9)})

    def test_semantic_and_hybrid_are_reported_unavailable_not_scored(self):
        report = json.loads(_run([*self.ARGS, "--channel", "lexical", "--channel", "semantic", "--channel", "hybrid"]))
        for channel in ("semantic", "hybrid"):
            rows = [c for c in report["configurations"] if c["channel"] == channel]
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["status"], "unavailable")
                self.assertIn("embedding backend", row["reason"])
                self.assertNotIn("passage", row)
        self.assertEqual(len(report["changes"]), 2)  # unavailable runs never enter comparisons

    def test_output_contains_no_queries_quotes_keys_paths_or_text(self):
        questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
        forbidden = {q["query"] for q in questions}
        for q in questions:
            for name in ("evidence", "traps", "qualifiers"):
                for span in q.get(name, []):
                    forbidden.update(span.values() if isinstance(next(iter(span.values())), str) else [])
                    forbidden.add(span.get("quote", "\0"))
        for record in json.loads(CORPUS.read_text(encoding="utf-8")):
            forbidden.update((record["zotero_attachment_key"], record["citation_key"], record["title"]))
        forbidden.discard("\0")
        forbidden = {f for f in forbidden if isinstance(f, str) and len(f) > 2}
        with tempfile.TemporaryDirectory() as tmp:
            for label, out in (
                ("json", _run([*self.ARGS, "--experiment-dir", tmp])),
                ("markdown", _run([a for a in self.ARGS if a != "--json"] + ["--experiment-dir", tmp])),
            ):
                for needle in forbidden | {tmp, Path(tmp).name}:
                    with self.subTest(format=label, needle=needle):
                        self.assertNotIn(needle, out)

    def test_experiment_dir_receives_only_experimental_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run([*self.ARGS, "--experiment-dir", tmp])
            names = sorted(p.name for p in Path(tmp).iterdir())
        self.assertEqual(
            names,
            [
                f"chars-6000-500{chunking.EXPERIMENT_SUFFIX}",
                f"sentence-40-0{chunking.EXPERIMENT_SUFFIX}",
                f"structural-40-6{chunking.EXPERIMENT_SUFFIX}",
            ],
        )

    def test_a_sweep_builds_ten_indexes_and_chunking_needs_a_corpus(self):
        report = json.loads(
            _run(["--questions", str(QUESTIONS), "--corpus", str(CORPUS), "--sweep", "sentence", "--k", "3", "--json"])
        )
        labels = [c["label"] for c in report["configurations"]]
        self.assertEqual(len(labels), 10)
        self.assertIn("sentence:384:48", labels)
        self.assertIn("sentence:128:0", labels)
        with self.assertRaises(SystemExit):
            _run(["--questions", str(QUESTIONS), "--chunking", "sentence:40:0"])

    def test_the_current_generation_is_scored_read_only_next_to_experiments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "corpus.jsonl"
            records = json.loads(CORPUS.read_text(encoding="utf-8"))
            jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            index_dir = root / "index"
            index_dir.mkdir()
            db = index_dir / "index.sqlite"
            build_fts_index(jsonl, db, chunk_chars=300, overlap_chars=40)
            before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in index_dir.iterdir()}
            report = json.loads(
                _run([
                    "--questions", str(QUESTIONS), "--corpus", str(CORPUS), "--db", str(db), "--label", "current",
                    "--passages", "--chunking", "sentence:40:0", "--k", "3", "--json",
                ])
            )
            after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in index_dir.iterdir()}
        self.assertEqual(before, after)
        current, candidate = report["configurations"]
        self.assertEqual((current["label"], candidate["label"]), ("current", "sentence:40:0"))
        self.assertIsNone(current["build"])
        self.assertEqual(current["passage"]["topk"]["3"]["locator_validity"], 1.0)
        self.assertEqual(report["changes"][0]["baseline"], "current/lexical/per-question")

    def test_legacy_paper_level_run_is_unchanged_by_the_new_fields(self):
        out = json.loads(_run(["--questions", str(FIXTURES / "synthetic.questions.json"), "--db", str(self._legacy_db()), "--json"]))
        self.assertNotIn("budgets", out)
        self.assertIn("configurations", out)

    def _legacy_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        jsonl = Path(tmp.name) / "c.jsonl"
        records = json.loads((FIXTURES / "synthetic_corpus.json").read_text(encoding="utf-8"))
        jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        db = Path(tmp.name) / "index.sqlite"
        build_fts_index(jsonl, db)
        return db


if __name__ == "__main__":
    unittest.main()
