# Public corpus journal register

Register of candidate venues for the S3b public PDF corpus
([plan](public-corpus-plan.md), [#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115)).
It is kept separate from `sources.json` so the sample can be extended later: add articles from a
`core` venue, promote a `reserve` venue, or add rows for a new field.

## Licence rule

Only one question matters here: **can derived material from the article live in this public,
MIT-licensed repository?** Derived material includes extracted text, evidence spans quoted in the
question set and small test fixtures.

- **Accepted:** CC BY (any version), CC0, public domain.
- **Rejected:** anything with NC, ND or SA, publisher-specific licences, and items with no
  verifiable licence.
- **The article's own licence decides**, not the journal's. Journals that let authors choose, or
  changed licence over time, are usable for their CC BY articles only.

Columns:

- **Role** — `core`: first sample (3 per field). `reserve`: replacement or extension candidate.
  `excluded`: checked and rejected; kept so it is not re-added.
- **Licence** — what the journal applies, with dates where it changed.
- **Usable** — `all`: every current article is CC BY. `CC BY only`: pick only articles whose own
  page shows CC BY. `no`: rejected.
- **Layout** — what the venue adds to layout diversity.
- **Verified** — date the licence was checked, or `—`.
- **Metric** — journal metric as reported in web-search results (JIF = Clarivate impact factor,
  SJR = Scimago Journal Rank, h5 = Google Scholar h5-index). Used only to rank venues within a field;
  metric databases themselves were not reachable from the checking environment.

`core` venues were chosen as the highest-ranked journals in each field that publish CC BY
articles; lower-ranked ones that were core earlier are now `reserve`.

Checks on 2026-10-02 were done by web search against journal, publisher and DOAJ pages. DOAJ and
some publisher sites could not be fetched directly from the checking environment, so some entries
rest on search-result excerpts of those pages. Every article's licence is still verified on its
own page before it is pinned.

## Clinical medicine

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| npj Digital Medicine | core | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | Nature style, figures, AI/clinical studies | 2026-10-02 | JIF 18.0 (2025) |
| PLOS Medicine | core | CC BY 4.0 | all | Two-column print PDF, structured abstract, tables | 2026-10-02 | — |
| BMC Medicine | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | Single-column, long methods | 2026-10-02 | — |
| Journal of Medical Internet Research | core | CC BY 4.0 | all | Single-column, survey and trial reports | 2026-10-02 | — |
| BMJ Open | excluded | CC BY-NC 4.0 default | no | — | 2026-10-02 | — |

## Physics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Advanced Photonics | core | CC BY 4.0 | all | Optics, figure-heavy | 2026-10-02 | JIF 19.5 (2025) |
| Journal of High Energy Physics | core | CC BY 4.0 (SCOAP3) | all | Long theory papers, collaboration author lists | 2026-10-02 | JIF 5.5 (2025) |
| Physical Review X | core | CC BY 4.0 | all | REVTeX two-column, dense equations | 2026-10-02 | JIF 15.7 (2025) |
| New Journal of Physics | reserve | CC BY 3.0 since Nov 2012; earlier CC BY-NC-SA 3.0 | CC BY only (2012+) | IOP single-column, equations | 2026-10-02 | — |
| SciPost Physics | reserve | CC BY 4.0 | all | Single-column LaTeX, long derivations | 2026-10-02 | — |
| Physical Review Research | reserve | CC BY 4.0 (expected) | — | REVTeX two-column | — | — |

## Chemistry

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| JACS Au | core | CC BY or CC BY-NC-ND 4.0 | CC BY only | ACS two-column | 2026-10-02 | JIF 8.5 (2024) |
| Chemical Science | core | CC BY 3.0 or CC BY-NC 3.0 | CC BY only | Reaction schemes, two-column | 2026-10-02 | — |
| Beilstein Journal of Organic Chemistry | core | CC BY 4.0 | all | Schemes, compound numbering, spectra | 2026-10-02 | — |
| Journal of Cheminformatics | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | Single-column, code and tables | 2026-10-02 | — |
| RSC Advances | reserve | CC BY 3.0 or CC BY-NC 3.0 (expected) | — | Two-column | — | — |

## Ecology and biology

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Nature Communications | core | CC BY 4.0 default since Oct 2014; CC BY-NC-ND on request | CC BY only | Nature style, methods at end | 2026-10-02 | JIF 18.1 (2025) |
| Communications Biology | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 | CC BY only | Nature style | 2026-10-02 | JIF 5.8 (2025) |
| Science Advances | reserve | CC BY-NC default; CC BY on request or funder mandate | CC BY only | AAAS style | 2026-10-02 | JIF 13.9 (2025) |
| eLife | core | CC BY 4.0 (some older CC BY 3.0, CC0) | all | Figure-heavy, figure supplements | 2026-10-02 | — |
| PLOS Biology | core | CC BY 4.0 | all | Two-column, multi-panel figures | 2026-10-02 | JIF 6.9 (2025) |
| Ecology and Evolution | reserve | CC BY since Aug 2012; earlier CC BY-NC | CC BY only (2012+) | Wiley single-column, species names | 2026-10-02 | — |
| PeerJ | reserve | CC BY 4.0 (expected) | — | Single-column | — | — |

## Statistics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Journal of Statistical Software | core | CC BY (version per article) | all | Code listings, R output | 2026-10-02 | SJR 3.2 |
| Electronic Journal of Statistics | core | CC BY 4.0 | all | Theorems, proofs, notation | 2026-10-02 | h5 30 |
| Bayesian Analysis | core | CC BY 4.0 | all | Equations, MCMC figures, discussion papers | 2026-10-02 | — |
| Journal of Machine Learning Research | reserve (already pinned ×2) | CC BY 4.0 (notice in PDF) | all | Long single-column | earlier pin | JIF 6.8 |

## Psychometrics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Psychometrika | core | fully OA from vol. 90 (2025); CC BY default, any CC licence allowed | CC BY only (2025+) | IRT, latent-variable models, heavy notation | 2026-10-02 | flagship psychometrics journal |
| Psych (MDPI) | core | CC BY 4.0 | all | MDPI template, IRT equations | 2026-10-02 | — |
| Large-scale Assessments in Education | core | CC BY 4.0 | all | PISA/TIMSS methodology, tables | 2026-10-02 | — |
| Journal of Intelligence (MDPI) | reserve | CC BY 4.0 | all | Factor models, path diagrams | 2026-10-02 | — |
| Measurement Instruments for the Social Sciences | reserve | CC BY 4.0 (expected) | — | Scale documentation, item tables | — | — |

## Computer science

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Transactions of the ACL (TACL) | core | CC BY 4.0 | all | ACL two-column, examples, tables | 2026-10-02 | — |
| LIPIcs (conference proceedings) | core | CC BY 4.0 | all | Theory papers, lemmas, pseudocode | 2026-10-02 | — |
| Journal of Artificial Intelligence Research | core | CC BY since May 2023 (vol. 77+); earlier JAIR License v1 | CC BY only (2023+) | Long single-column | 2026-10-02 | SJR Q1 |
| Transactions on Machine Learning Research | reserve | CC BY 4.0 (expected) | — | OpenReview format | — | — |

## Economics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Economics (De Gruyter, formerly E-Journal) | core | CC BY 4.0 | all | Working-paper style | 2026-10-02 | — |
| IZA Journal of Labor Economics | core | CC BY 4.0 (series closed Dec 2023; archive stays online) | all | Wide regression tables | 2026-10-02 | — |
| Journal for Labour Market Research | core | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | Labour-market data, tables | 2026-10-02 | — |
| Theoretical Economics | excluded | CC BY-NC | no | — | 2026-10-02 | — |
| Quantitative Economics | excluded | CC BY-NC | no | — | 2026-10-02 | — |

## Materials and engineering

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Science and Technology of Advanced Materials | core | CC BY or CC BY-NC | CC BY only | T&F, reviews and micrographs | 2026-10-02 | JIF 6.6 (2025) |
| Materials & Design | core | CC BY, CC BY-NC or CC BY-NC-ND (author choice) | CC BY only | Elsevier two-column, micrographs, units | 2026-10-02 | — |
| npj Computational Materials | core | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | Nature style, simulation figures | 2026-10-02 | — |
| IEEE Access | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | IEEE two-column, block diagrams | 2026-10-02 | — |
| Scientific Reports | reserve | CC BY 4.0 (expected) | — | Multidisciplinary | — | — |

## Psychology

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Collabra: Psychology | core | CC BY 4.0 | all | APA-like, preregistration notes | 2026-10-02 | — |
| Frontiers in Psychology | core | CC BY 4.0 | all | Frontiers two-column | 2026-10-02 | — |
| Meta-Psychology | core | CC BY | all | Single-column, reproducibility reports | 2026-10-02 | — |
| International Review of Social Psychology | reserve | CC BY 4.0 (expected) | — | Ubiquity Press template | — | — |

## Education

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Smart Learning Environments | core | CC BY 4.0 | all | Springer single-column | 2026-10-02 | SJR 4.2 |
| International Journal of STEM Education | core | CC BY 4.0 or CC BY-NC-ND 4.0 | CC BY only | Springer single-column | 2026-10-02 | SJR 3.3 |
| Frontiers in Education | reserve | CC BY 4.0 | all | Frontiers two-column | 2026-10-02 | — |
| Education Sciences (MDPI) | reserve | CC BY 4.0 | all | MDPI template | 2026-10-02 | — |
| International Journal of Educational Technology in Higher Education | core | CC BY 4.0 | all | Springer single-column | 2026-10-02 | SJR 7.9 |
| NEPS Survey Papers | reserve | no licence statement found | — | Survey documentation, codebook tables; some German | 2026-10-02 | — |
| AERA Open | excluded | CC BY-NC default | no | — | 2026-10-02 | — |
| Journal for Educational Research Online (JERO) | excluded | CC BY-NC-SA 4.0 | no | — | 2026-10-02 | — |
| Frontline Learning Research | excluded | CC BY-NC-ND | no | — | 2026-10-02 | — |

## Sociology and political science

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| British Journal of Political Science | core | OA since Jul 2024; any CC licence | CC BY only (2024+) | Cambridge single-column, regression tables | 2026-10-02 | SJR 3.6 |
| Sociological Science | core | CC BY | all | Single-column, regression tables | 2026-10-02 | — |
| Politics and Governance | core | CC BY 4.0 | all | Cogitatio template | 2026-10-02 | — |
| Social Inclusion | reserve | CC BY 4.0 | all | Cogitatio template | 2026-10-02 | — |
| Socius | reserve | CC BY or CC BY-NC (mostly NC) | CC BY only | SAGE, data visualizations | 2026-10-02 | — |
| Research & Politics | excluded | CC BY-NC mostly | no | — | 2026-10-02 | — |

## Law

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| German Law Journal | core | CC BY 4.0 since 2019 | CC BY only (2019+) | Footnote-heavy, case citations | 2026-10-02 | — |
| Utrecht Law Review | core | CC BY 4.0 | all | Single-column, footnotes | 2026-10-02 | — |
| Laws (MDPI) | core | CC BY 4.0 | all | MDPI template, legal citations | 2026-10-02 | — |

## Humanities and linguistics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Semantics and Pragmatics | core | CC BY 4.0 | all | Formal semantics, logical notation, examples | 2026-10-02 | diamond; JIF 1.1 (2023) |
| Glossa | core | CC BY 4.0 | all | Interlinear glosses, numbered examples | 2026-10-02 | JIF 0.8 (2024) |
| Open Library of Humanities journal | reserve | CC BY 4.0 default | CC BY only | Long prose, images | 2026-10-02 | — |
| Journal of Open Humanities Data | core | CC BY 4.0 | all | Short data papers | 2026-10-02 | — |
| Language Science Press (books) | reserve | CC BY 4.0 (expected) | — | Monographs; also the long-book candidate | — | — |
| Digital Humanities Quarterly | excluded | CC BY-ND 4.0 default (some CC BY/CC0) | no | — | 2026-10-02 | — |

## Non-English venues

Not yet verified. Each needs a CC BY licence under the rule above.

| Venue | Language | Licence (expected) | Field | Verified |
| --- | --- | --- | --- | --- |
| Zeitschrift für Soziologie | de | CC BY 4.0 | Sociology | — |
| Forum Qualitative Sozialforschung (FQS) | de (multilingual) | CC BY 4.0 | Social science methods | — |
| OpenEdition Journals (e.g. *Sociologie*, *Formation emploi*) | fr | varies by journal | Sociology/education | — |
| Économie et Statistique / Economics and Statistics (INSEE) | fr | check | Economics | — |
| Revista Española de Investigaciones Sociológicas (REIS) | es | check | Sociology | — |
| SciELO (journal collection) | es/pt | varies by journal | Any | — |

## Extra formats

Not yet verified.

| Source | Format | Licence (expected) | Notes | Verified |
| --- | --- | --- | --- | --- |
| Internet Archive / Biodiversity Heritage Library | Scans | Public domain (jurisdiction-dependent) | One scan already pinned | — |
| Zenodo | Slide decks, reports | per record; pick CC BY | Filter by resource type | — |
| Language Science Press / OAPEN | Long monograph | CC BY 4.0 (OAPEN varies) | 100+ pages | — |

## Extending the sample

- New article from a `core` venue: pick per the plan's article rules, confirm CC BY on the article
  page, add to `sources.json`.
- Promote a `reserve` venue: verify its licence, set `Usable` and `Verified`.
- New field: add a section with three `core` venues and at least one `reserve`.
- A venue that drops CC BY moves to `excluded` with a note; its already-pinned CC BY articles stay
  valid.
