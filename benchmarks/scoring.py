"""Reusable scoring for the OCR classification benchmark.

The benchmark freezes the classifier's *inputs*, not a stale prediction: each committed crop
carries its geometry and the two neighbouring Markdown lines (``geometry.json``), which is
everything ``classify_crop`` reads. This module rebuilds a faithful ``CropRef`` from that committed
data and scores whatever the *current* classifier returns -- so the benchmark re-runs the live
algorithm on every change, with no PDF, no model and no network.

Two vocabularies meet here. Ground-truth labels are authored in reader terms
(``equation | figure | table | decoration``); ``classify_crop`` returns routing terms
(``formula | figure | table | skip``) -- the OCR task prompt it would pick. ``is_correct`` bridges
them and encodes the project's routing tolerance:

  - ``equation`` is satisfied only by ``formula`` (a misrouted equation loses its notation).
  - ``table`` is satisfied only by ``table``.
  - ``figure`` is satisfied by ``figure`` (its pixels get described) *or* ``skip`` (kept as an
    opaque image) -- both keep a non-math crop out of a math prompt, which is the whole risk.
  - ``decoration`` is satisfied by ``figure`` or ``skip``: routing page furniture to figure
    description is harmless, but calling it an equation or a table is a real error. This mirrors
    the decoration-tolerant rule the synthetic corpus tests use.

Grow the benchmark by adding tier directories with the same ``crops/<key>/geometry.json`` +
``labels.json`` shape; ``load_tier`` reads any of them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

TIER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TIER_ROOT.parent / "src"))

from zotero_pdf_text.image_ocr import (  # noqa: E402  (path set up above)
    CLASS_FIGURE,
    CLASS_FORMULA,
    CLASS_SKIP,
    CLASS_TABLE,
    CropRef,
    read_png_size,
)

# Which routing classes satisfy each reader-facing ground-truth label.
_ACCEPTED: dict[str, frozenset[str]] = {
    "equation": frozenset({CLASS_FORMULA}),
    "table": frozenset({CLASS_TABLE}),
    "figure": frozenset({CLASS_FIGURE, CLASS_SKIP}),
    "decoration": frozenset({CLASS_FIGURE, CLASS_SKIP}),
}


def is_correct(expected_label: str, predicted_class: str) -> bool:
    """True if the router's class is an acceptable destination for the labelled crop."""
    try:
        return predicted_class in _ACCEPTED[expected_label]
    except KeyError as exc:  # a typo'd label must fail loudly, never silently pass
        raise ValueError(f"unknown ground-truth label {expected_label!r}") from exc


@dataclass(frozen=True)
class BenchmarkCrop:
    """One labelled crop, reconstructed from committed benchmark data (no PDF needed)."""

    crop_id: str
    tier: str
    expected: str
    ref: CropRef

    @property
    def key(self) -> str:
        """The labels.json key: ``<tier>/<crop_id>.png``."""
        return f"{self.tier}/{self.crop_id}.png"


def _reconstruct_ref(png_path: Path, text_before: str, text_after: str) -> CropRef:
    """Rebuild the CropRef classify_crop sees: real dimensions/bytes + the stored text context."""
    size = read_png_size(png_path)
    width, height = size if size else (0, 0)
    try:
        byte_size = png_path.stat().st_size
    except OSError:
        byte_size = 0
    return CropRef(
        span=(0, 0),
        markup="",
        link=png_path.name,
        png_path=png_path,
        width=width,
        height=height,
        text_before=text_before,
        text_after=text_after,
        byte_size=byte_size,
    )


def load_tier(tier: str) -> list[BenchmarkCrop]:
    """Load every labelled crop of one tier as reconstructed CropRefs.

    ``tier`` names a directory under ``benchmarks/`` holding ``crops/<key>/geometry.json`` and a
    ``labels.json`` mapping ``<key>/<crop>.png`` to a ground-truth label. Crops without a label are
    skipped (a partially-labelled, still-growing tier is not an error).
    """
    tier_dir = TIER_ROOT / tier
    labels = json.loads((tier_dir / "labels.json").read_text(encoding="utf-8"))["crops"]
    crops: list[BenchmarkCrop] = []
    for geometry_file in sorted((tier_dir / "crops").glob("*/geometry.json")):
        key_dir = geometry_file.parent.name
        for entry in json.loads(geometry_file.read_text(encoding="utf-8")):
            key = f"{key_dir}/{entry['id']}.png"
            expected = labels.get(key)
            if expected is None:
                continue
            ref = _reconstruct_ref(
                geometry_file.parent / f"{entry['id']}.png",
                entry.get("text_before", ""),
                entry.get("text_after", ""),
            )
            crops.append(BenchmarkCrop(entry["id"], key_dir, expected, ref))
    return crops


@dataclass(frozen=True)
class Score:
    """Aggregate result of scoring a predictor over a set of crops."""

    total: int
    correct: int
    per_label: dict[str, tuple[int, int]]  # label -> (correct, total)
    errors: list[tuple[str, str, str]]     # (key, expected_label, predicted_class)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def label_recall(self, label: str) -> float:
        correct, total = self.per_label.get(label, (0, 0))
        return correct / total if total else 0.0

    def report(self) -> str:
        lines = [f"overall {self.correct}/{self.total} = {self.accuracy:.1%}"]
        for label in sorted(self.per_label):
            c, t = self.per_label[label]
            lines.append(f"  {label:12s} {c}/{t}")
        for key, exp, got in self.errors:
            lines.append(f"  MISS {key}: labelled {exp!r}, routed {got!r}")
        return "\n".join(lines)


def score(crops: list[BenchmarkCrop], predict) -> Score:
    """Score ``predict(ref) -> class`` over ``crops`` with the routing-tolerance rules."""
    correct = 0
    per_label: dict[str, list[int]] = {}
    errors: list[tuple[str, str, str]] = []
    for crop in crops:
        predicted = predict(crop.ref)
        ok = is_correct(crop.expected, predicted)
        bucket = per_label.setdefault(crop.expected, [0, 0])
        bucket[1] += 1
        if ok:
            correct += 1
            bucket[0] += 1
        else:
            errors.append((crop.key, crop.expected, predicted))
    return Score(
        total=len(crops),
        correct=correct,
        per_label={k: (v[0], v[1]) for k, v in per_label.items()},
        errors=errors,
    )
