"""Retrieval-quality harness (benchmarks/retrieval.py, roadmap step S1)."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import retrieval

from zotero_pdf_text.fts import build_fts_index

FIXTURES = Path(__file__).parent / "fixtures" / "retrieval"
CORPUS = FIXTURES / "synthetic_corpus.json"
QUESTIONS = FIXTURES / "synthetic.questions.json"


def _run(argv: list[str]) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        retrieval.main(argv)
    return buffer.getvalue()


def _snapshot(root: Path) -> dict[str, tuple[int, int, str | None]]:
    state = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.name == "index.sqlite" else None
        state[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns, digest)
    return state


def _question_file(path: Path, questions: list[dict], **overrides) -> Path:
    data = {"format": retrieval.FORMAT, "version": 1, "synthetic": True, "questions": questions}
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class MetricTests(unittest.TestCase):
    RANKED = [("A1", "k1"), ("A2", "k2"), ("A3", "k2"), ("A4", "k4")]

    def test_hit_at_rank_one(self):
        score = retrieval.score_question(self.RANKED, [{"attachment_key": "A1"}], [1, 5])
        self.assertEqual(score.recall, {1: 1.0, 5: 1.0})
        self.assertEqual((score.rr, score.first_rank, score.found), (1.0, 1, 1))

    def test_hit_below_the_cutoff_counts_only_for_larger_k(self):
        score = retrieval.score_question(self.RANKED, [{"attachment_key": "A4"}], [2, 4])
        self.assertEqual(score.recall, {2: 0.0, 4: 1.0})
        self.assertEqual(score.rr, 0.25)

    def test_miss(self):
        score = retrieval.score_question(self.RANKED, [{"attachment_key": "ZZ"}], [5])
        self.assertEqual((score.recall, score.rr, score.first_rank, score.found), ({5: 0.0}, 0.0, None, 0))

    def test_partial_recall_with_two_expected(self):
        score = retrieval.score_question(
            self.RANKED, [{"attachment_key": "A2"}, {"attachment_key": "ZZ"}], [5]
        )
        self.assertEqual((score.recall[5], score.rr, score.found), (0.5, 0.5, 1))

    def test_citation_key_covering_two_attachments_counts_once(self):
        score = retrieval.score_question(self.RANKED, [{"citation_key": "k2"}], [5])
        self.assertEqual((score.recall[5], score.first_rank, score.found), (1.0, 2, 1))

    def test_citation_key_is_case_sensitive(self):
        score = retrieval.score_question(self.RANKED, [{"citation_key": "K2"}], [5])
        self.assertEqual(score.found, 0)

    def test_aggregate_means_and_rounding(self):
        scores = [
            retrieval.score_question(self.RANKED, [{"attachment_key": "A1"}], [5]),
            retrieval.score_question(self.RANKED, [{"attachment_key": "A3"}], [5]),
            retrieval.score_question(self.RANKED, [{"attachment_key": "ZZ"}], [5]),
        ]
        self.assertEqual(
            retrieval.aggregate(scores), {"n": 3, "recall": {"5": 0.667}, "mrr": 0.444}
        )

    def test_outcome_order_treats_no_hit_as_worst(self):
        hit_late = retrieval.score_question(self.RANKED, [{"attachment_key": "A4"}], [5])
        miss = retrieval.score_question(self.RANKED, [{"attachment_key": "ZZ"}], [5])
        self.assertLess(retrieval._outcome_key(hit_late), retrieval._outcome_key(miss))


class ValidationTests(unittest.TestCase):
    def _load(self, questions=None, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            path = _question_file(Path(tmp) / "x.questions.json", questions or [self._q()], **overrides)
            return retrieval.load_questions(path)

    @staticmethod
    def _q(**fields):
        base = {"id": "q1", "query": "shrinkage", "expected": [{"attachment_key": "A"}]}
        base.update(fields)
        return base

    def test_valid_file_loads(self):
        questions, synthetic = self._load()
        self.assertEqual([q.id for q in questions], ["q1"])
        self.assertEqual(questions[0].type, "untyped")
        self.assertTrue(synthetic)

    def test_rejections(self):
        cases = {
            "format": dict(questions=None, format="other"),
            "version": dict(questions=None, version=2),
            "duplicate id": dict(questions=[self._q(), self._q()]),
            "empty expected": dict(questions=[self._q(expected=[])]),
            "both keys": dict(
                questions=[self._q(expected=[{"attachment_key": "A", "citation_key": "b"}])]
            ),
            "neither key": dict(questions=[self._q(expected=[{}])]),
            "parent key": dict(questions=[self._q(expected=[{"parent_key": "P"}])]),
            "bad mode": dict(questions=[self._q(search_mode="fuzzy")]),
            "too many terms": dict(questions=[self._q(query=" ".join(["w"] * 21))]),
            "long term": dict(questions=[self._q(query="x" * 65)]),
            "blank query": dict(questions=[self._q(query="  ")]),
        }
        for name, kwargs in cases.items():
            with self.subTest(name), self.assertRaises(SystemExit):
                self._load(**kwargs)

    def test_label_count_must_match_dbs(self):
        with self.assertRaises(SystemExit):
            retrieval._labels(["A", "B"], 1)
        self.assertEqual(retrieval._labels(None, 3), ["A", "B", "C"])


class FixtureRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        jsonl = cls.root / "corpus.jsonl"
        records = json.loads(CORPUS.read_text(encoding="utf-8"))
        jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        cls.index_dir = cls.root / "index"
        cls.index_dir.mkdir()
        cls.db = cls.index_dir / "index.sqlite"
        build_fts_index(jsonl, cls.db)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _report(self):
        out = _run(
            ["--questions", str(QUESTIONS), "--db", str(self.db),
             "--mode", "all_terms", "--mode", "any_terms", "--json"]
        )
        return json.loads(out), out

    def test_aggregates_and_changed_questions(self):
        report, _ = self._report()
        all_terms, any_terms = report["configurations"]
        self.assertEqual(report["k"], [5, 10, 20])
        self.assertEqual((all_terms["label"], all_terms["mode"]), ("A", "all_terms"))
        self.assertEqual(
            all_terms["overall"], {"n": 8, "recall": {"5": 0.562, "10": 0.562, "20": 0.562}, "mrr": 0.562}
        )
        self.assertEqual(
            any_terms["overall"], {"n": 8, "recall": {"5": 0.688, "10": 0.688, "20": 0.688}, "mrr": 0.625}
        )
        self.assertEqual(sorted(all_terms["by_type"]), ["citation", "conceptual", "exact_phrase", "untyped"])
        self.assertEqual(all_terms["by_type"]["conceptual"]["mrr"], 0.833)
        self.assertEqual(all_terms["by_type"]["untyped"]["n"], 1)
        self.assertEqual(all_terms["not_in_index"], ["q06"])
        self.assertEqual(all_terms["invalid_queries"], [])
        (change,) = report["changes"]
        self.assertEqual((change["baseline"], change["against"]), ("A/all_terms", "A/any_terms"))
        self.assertEqual([r["id"] for r in change["improved"]], ["q07"])
        self.assertEqual(change["improved"][0]["baseline_rank"], None)
        self.assertEqual(change["improved"][0]["rank"], 1)
        self.assertEqual([r["id"] for r in change["regressed"]], ["q08"])
        self.assertEqual((change["regressed"][0]["baseline_rank"], change["regressed"][0]["rank"]), (1, 2))

    def test_per_question_mode_is_the_default(self):
        report = json.loads(_run(["--questions", str(QUESTIONS), "--db", str(self.db), "--json"]))
        (config,) = report["configurations"]
        self.assertEqual(config["mode"], "per-question")
        self.assertEqual(config["overall"]["mrr"], 0.562)

    def test_two_dbs_get_labels_and_compare(self):
        out = _run(
            ["--questions", str(QUESTIONS), "--db", str(self.db), "--db", str(self.db),
             "--label", "old", "--label", "new", "--mode", "all_terms", "--json"]
        )
        report = json.loads(out)
        self.assertEqual([c["label"] for c in report["configurations"]], ["old", "new"])
        self.assertEqual(report["changes"][0]["improved"] + report["changes"][0]["regressed"], [])

    def test_the_run_never_touches_the_index_directory(self):
        before = _snapshot(self.index_dir)
        _run(["--questions", str(QUESTIONS), "--db", str(self.db), "--mode", "all_terms", "--mode", "any_terms"])
        self.assertEqual(_snapshot(self.index_dir), before)

    def test_output_contains_no_queries_keys_or_paths(self):
        questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
        forbidden = {q["query"] for q in questions}
        for q in questions:
            forbidden.update(v for entry in q["expected"] for v in entry.values())
        forbidden.update(
            r[k] for r in json.loads(CORPUS.read_text(encoding="utf-8"))
            for k in ("zotero_attachment_key", "citation_key", "title")
        )
        forbidden.update({str(self.db), str(self.root), self.root.name})
        args = ["--questions", str(QUESTIONS), "--db", str(self.db), "--mode", "all_terms", "--mode", "any_terms"]
        for label, out in (("json", _run(args + ["--json"])), ("markdown", _run(args))):
            for needle in forbidden:
                with self.subTest(format=label, needle=needle):
                    self.assertNotIn(needle, out)

    def test_an_unopenable_index_reports_no_path(self):
        missing = self.root / "secret-name" / "index.sqlite"
        with self.assertRaises(SystemExit) as caught:
            _run(["--questions", str(QUESTIONS), "--db", str(missing)])
        self.assertNotIn("secret-name", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
