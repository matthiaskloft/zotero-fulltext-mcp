# Preprint benchmark tier — attribution

The real-article tier of the OCR benchmark uses image crops extracted from **open first-author
preprints by Matthias Kloft**, included here **with the author's permission** (the author is a
maintainer of this project). What is committed is deliberately minimal: the small extracted image
crops, their hand-reviewed labels, and — per crop — the two neighbouring Markdown lines the
classifier reads (a caption or picture marker, capped at 200 characters each in `geometry.json`),
which the offline CI harness needs to re-run classification faithfully. No running prose beyond
those two short lines, and no full source PDF, is redistributed: the PDFs are fetched on first use
into the git-ignored `.cache/` and never committed. Every tracked byte is the author's own
open-access work.

Regenerate crops with `python tools/build_preprint_benchmark.py`. Source identifiers live in
`sources.json`.

## Sources

| Key | Citation |
|-----|----------|
| `interval_truth` | Kloft, M., Siepe, B. S., & Heck, D. W. (2024). *The Interval Truth Model: A consensus model for continuous bounded interval responses.* PsyArXiv. https://osf.io/dzvw2 |
| `dirichlet_dual` | Kloft, M., Hartmann, R., Voss, A., & Heck, D. W. (2023). *The Dirichlet Dual Response Model: An item response model for continuous bounded interval responses.* Psychometrika. https://doi.org/10.1007/s11336-023-09924-7 |
| `interval_consensus` | Kloft, M., Siepe, B. S., & Heck, D. W. (2026). *The Interval Consensus Model: Aggregating continuous bounded interval responses.* Psychometrika. https://doi.org/10.1017/psy.2025.10058 |
| `discriminant_validity` | Kloft, M., & Heck, D. W. (2025). *Discriminant validity of interval response formats: Investigating the dimensional structure of interval widths.* Educational and Psychological Measurement. https://doi.org/10.1177/00131644241283400 |
| `dual_range_slider` | Kloft, M., Snijder, J., & Heck, D. W. (2024). *Measuring the variability of personality traits with interval responses: Psychometric properties of the dual-range slider response format.* Behavior Research Methods. https://doi.org/10.3758/s13428-024-02394-4 |

Crops are used solely as classification/recognition test fixtures. If you are not the author and
wish to reuse them, consult the licence of each preprint on its source page.
