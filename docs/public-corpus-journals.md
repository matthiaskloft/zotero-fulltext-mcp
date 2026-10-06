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

The first pass on 2026-10-02 rested partly on web-search excerpts. A second pass the same day
queried the DOAJ API by ISSN for every venue below (`DOAJ:` in the Licence column) and opened the
selected articles' own pages ([article list](public-corpus-articles.md)). DOAJ shows only a
journal's current policy; several venues used other licences earlier, which the article checks
exposed and the rows below now state. Venues that are not in DOAJ say so.

## Clinical medicine

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| npj Digital Medicine | core | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice); DOAJ: CC BY, CC BY-NC-ND | CC BY only | Nature style, figures, AI/clinical studies | 2026-10-03 | JIF 18.0 (2025) |
| PLOS Medicine | core | CC BY 4.0; DOAJ: CC BY | all | Two-column print PDF, structured abstract, tables | 2026-10-03 | — |
| BMC Medicine | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | Single-column, long methods | 2026-10-02 | — |
| Journal of Medical Internet Research | core | CC BY (2.0 on the 2004–2005 articles checked); DOAJ: CC BY. PDFs only reach a browser | all | Single-column, survey and trial reports | 2026-10-03 | — |
| BMJ Open | excluded | CC BY-NC 4.0 default | no | — | 2026-10-02 | — |

## Physics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Advanced Photonics | core | CC BY 4.0; DOAJ: CC BY. SPIE pages and PDFs are blocked to scripts and the browser; articles need a repository copy | all | Optics, figure-heavy | 2026-10-03 | JIF 19.5 (2025) |
| Journal of High Energy Physics | core | CC BY 4.0 (SCOAP3); DOAJ: CC BY | all | Long theory papers, collaboration author lists | 2026-10-03 | JIF 5.5 (2025) |
| Physical Review X | core | CC BY (3.0 on the 2015–2016 articles checked); DOAJ: CC BY | all | REVTeX two-column, dense equations | 2026-10-03 | JIF 15.7 (2025) |
| New Journal of Physics | reserve | CC BY 3.0 since Nov 2012; earlier CC BY-NC-SA 3.0 | CC BY only (2012+) | IOP single-column, equations | 2026-10-02 | — |
| SciPost Physics | reserve | CC BY 4.0 | all | Single-column LaTeX, long derivations | 2026-10-02 | — |
| Physical Review Research | reserve | DOAJ: CC BY | all | REVTeX two-column | 2026-10-03 | — |

## Chemistry

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| JACS Au | core | CC BY or CC BY-NC-ND 4.0; DOAJ: same. The two most cited articles are NC-ND | CC BY only | ACS two-column | 2026-10-03 | JIF 8.5 (2024) |
| Chemical Science | core | CC BY 3.0 or CC BY-NC 3.0; DOAJ: CC BY, CC BY-NC | CC BY only | Reaction schemes, two-column | 2026-10-03 | — |
| Beilstein Journal of Organic Chemistry | core | CC BY (2.0 on the 2010 and 2013 articles checked); DOAJ: CC BY | all | Schemes, compound numbering, spectra | 2026-10-03 | — |
| Journal of Cheminformatics | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | Single-column, code and tables | 2026-10-02 | — |
| RSC Advances | reserve | DOAJ: CC BY, CC BY-NC | CC BY only | Two-column | 2026-10-03 | — |

## Ecology and biology

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Nature Communications | core | CC BY 4.0 default since Oct 2014; CC BY-NC-ND on request | CC BY only | Nature style, methods at end | 2026-10-02 | JIF 18.1 (2025) |
| Communications Biology | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 | CC BY only | Nature style | 2026-10-02 | JIF 5.8 (2025) |
| Science Advances | reserve | CC BY-NC default; CC BY on request or funder mandate | CC BY only | AAAS style | 2026-10-02 | JIF 13.9 (2025) |
| eLife | core | CC BY 4.0 (some older CC BY 3.0, CC0) | all | Figure-heavy, figure supplements | 2026-10-02 | — |
| PLOS Biology | core | CC BY 4.0 | all | Two-column, multi-panel figures | 2026-10-02 | JIF 6.9 (2025) |
| Ecology and Evolution | reserve | CC BY since Aug 2012; earlier CC BY-NC | CC BY only (2012+) | Wiley single-column, species names | 2026-10-02 | — |
| PeerJ | reserve | DOAJ: CC BY | all | Single-column | 2026-10-03 | — |

## Statistics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Journal of Statistical Software | core | CC BY (3.0 on checked articles); DOAJ: CC BY | all | Code listings, R output | 2026-10-03 | SJR 3.2 |
| Electronic Journal of Statistics | core | CC BY 4.0 from about vol. 11 (2017); 2008–2016 articles checked carry IMS/Bernoulli Society copyright only. DOAJ: CC BY (current) | CC BY only (2017+) | Theorems, proofs, notation | 2026-10-03 | h5 30 |
| Bayesian Analysis | core | CC BY 4.0 from about 2018; 2006–2011 articles checked carry ISBA copyright only. Not in DOAJ | CC BY only (2018+) | Equations, MCMC figures, discussion papers | 2026-10-03 | — |
| Journal of Machine Learning Research | reserve (already pinned ×2) | CC BY 4.0 (notice in PDF) | all | Long single-column | earlier pin | JIF 6.8 |

## Psychometrics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Psychometrika | core | fully OA from vol. 90 (2025); CC BY default, any CC licence allowed; older hybrid articles vary (Sijtsma 2009 is CC BY-NC). Not in DOAJ | CC BY only | IRT, latent-variable models, heavy notation | 2026-10-03 | flagship psychometrics journal |
| Psych (MDPI) | core | CC BY 4.0; DOAJ: CC BY | all | MDPI template, IRT equations | 2026-10-03 | — |
| Large-scale Assessments in Education | core | CC BY 4.0; DOAJ: CC BY | all | PISA/TIMSS methodology, tables | 2026-10-03 | — |
| Journal of Intelligence (MDPI) | reserve | CC BY 4.0 | all | Factor models, path diagrams | 2026-10-02 | — |
| Measurement Instruments for the Social Sciences | reserve | DOAJ: CC BY | all | Scale documentation, item tables | 2026-10-03 | — |

## Computer science

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Transactions of the ACL (TACL) | core | CC BY 4.0; DOAJ: CC BY. MIT Press pages block scripts; ACL Anthology hosts the PDFs | all | ACL two-column, examples, tables | 2026-10-03 | — |
| LIPIcs (conference proceedings) | core | CC BY 3.0, later 4.0; 2010 volumes CC BY-NC-ND 3.0 (DataCite rights). DOAJ: CC BY | CC BY only | Theory papers, lemmas, pseudocode | 2026-10-03 | — |
| Journal of Artificial Intelligence Research | core | CC BY since vol. 77 (May 2023), stated in the PDF; earlier JAIR License v1. DOAJ: CC BY | CC BY only (vol. 77+) | Long single-column | 2026-10-03 | SJR Q1 |
| Transactions on Machine Learning Research | reserve | DOAJ: CC BY | all | OpenReview format | 2026-10-03 | — |

## Economics

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Economics (De Gruyter, formerly E-Journal) | core | CC BY-NC 2.0 DE in early volumes (checked 2007, 2008, 2012); CC BY 4.0 from at least 2017. DOAJ: CC BY (current) | CC BY only (later volumes) | Working-paper style | 2026-10-03 | — |
| IZA Journal of Labor Economics | core | CC BY (2.0 early, 4.0 later; series closed Dec 2023). Not in DOAJ | all | Wide regression tables | 2026-10-03 | — |
| Journal for Labour Market Research | core | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice); DOAJ: same. Pre-2016 articles have no CC licence | CC BY only (2016+) | Labour-market data, tables | 2026-10-03 | — |
| Theoretical Economics | excluded | CC BY-NC | no | — | 2026-10-02 | — |
| Quantitative Economics | excluded | CC BY-NC | no | — | 2026-10-02 | — |

## Materials and engineering

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Science and Technology of Advanced Materials | core | CC BY or CC BY-NC (DOAJ: same); IOP-era articles vary (2010 review © NIMS only, 2015 review CC BY 3.0) | CC BY only | T&F, reviews and micrographs | 2026-10-03 | JIF 6.6 (2025) |
| Materials & Design | dropped 2026-10-06 | CC BY, CC BY-NC or CC BY-NC-ND (author choice); DOAJ: same. ScienceDirect blocks page and PDF access, repository copies carry conflicting rights labels | CC BY only | Elsevier two-column, micrographs, units | 2026-10-03 | — |
| npj Computational Materials | core | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice); DOAJ: same | CC BY only | Nature style, simulation figures | 2026-10-03 | — |
| IEEE Access | reserve | CC BY 4.0 or CC BY-NC-ND 4.0 (author choice) | CC BY only | IEEE two-column, block diagrams | 2026-10-02 | — |
| Scientific Reports | reserve | CC BY 4.0 or CC BY-NC-ND 4.0; DOAJ: same. PDFs reachable through PubMed Central | CC BY only | Multidisciplinary | 2026-10-03 | — |

## Psychology

| Journal | Role | Licence | Usable | Layout | Verified | Metric |
| --- | --- | --- | --- | --- | --- | --- |
| Collabra: Psychology | core | CC BY 4.0; DOAJ: CC BY. UC Press pages and PDFs sit behind an interactive challenge; articles need a repository copy | all | APA-like, preregistration notes | 2026-10-03 | — |
| Frontiers in Psychology | core | CC BY 4.0 | all | Frontiers two-column | 2026-10-02 | — |
| Meta-Psychology | core | CC BY | all | Single-column, reproducibility reports | 2026-10-02 | — |
| International Review of Social Psychology | reserve | DOAJ: CC BY | all | Ubiquity Press template | 2026-10-03 | — |

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
| Semantics and Pragmatics | core | CC BY-NC 3.0 in vols. 1–6 (to 2013, per the PDFs; the site text says CC BY); CC BY 3.0 from vol. 7 (2014). DOAJ: CC BY (current) | CC BY only (2014+) | Formal semantics, logical notation, examples | 2026-10-03 | diamond; JIF 1.1 (2023) |
| Glossa | core | CC BY 4.0 | all | Interlinear glosses, numbered examples | 2026-10-02 | JIF 0.8 (2024) |
| Open Library of Humanities journal | reserve | CC BY 4.0 default | CC BY only | Long prose, images | 2026-10-02 | — |
| Journal of Open Humanities Data | core | CC BY 4.0 | all | Short data papers | 2026-10-02 | — |
| Language Science Press (books) | reserve | CC BY 4.0 (checked on one book record and its imprint page) | CC BY only | Monographs; also the long-book candidate | 2026-10-03 | — |
| Digital Humanities Quarterly | excluded | CC BY-ND 4.0 default (some CC BY/CC0) | no | — | 2026-10-02 | — |

## Non-English venues

Checked 2026-10-03. Selected articles are in the [article list](public-corpus-articles.md).

| Venue | Language | Licence | Usable | Field | Verified |
| --- | --- | --- | --- | --- | --- |
| Forum Qualitative Sozialforschung (FQS) | de (multilingual) | CC BY 4.0 on the article page; DOAJ: CC BY | all | Social science methods | 2026-10-03 |
| Zeitschrift für Erziehungswissenschaft (Springer) | de | CC BY 4.0 on open-access articles; not in DOAJ | CC BY only | Education | 2026-10-03 |
| Comptes Rendus (Académie des sciences, Centre Mersenne) | fr (and en) | CC BY 4.0 on the article page; DOAJ: CC BY | all | Sciences | 2026-10-03 |
| Cadernos de Saúde Pública (SciELO) | es/pt | CC BY 4.0 on the article page; DOAJ: CC BY | all | Public health | 2026-10-03 |
| Ecología Austral | es | CC BY 3.0 on the 2018 article (policy text cites 4.0); DOAJ: CC BY | all | Ecology | 2026-10-03 |
| Zeitschrift für Soziologie | de | No CC licence on the 2021 article page checked; not in DOAJ | no | Sociology | 2026-10-03 |
| OpenEdition Journals (Cybergeo, Semen, …) | fr | CC BY 4.0 for the text only, but PDFs are subscriber-only (HTTP 401) | no (no open PDF) | Various | 2026-10-03 |
| Économie et Statistique (Persée) | fr | Persée terms, not CC BY; not in DOAJ | no | Economics | 2026-10-03 |
| Revista Española de Investigaciones Sociológicas (REIS) | es | not in DOAJ; not checked further | — | Sociology | — |

## Extra formats

| Source | Format | Licence | Notes | Verified |
| --- | --- | --- | --- | --- |
| NASA Technical Reports Server (NACA) | Scans | US federal publications of the 1920s, public domain in the US; NTRS: public use permitted | Short items with no or garbled text layer | 2026-10-03 |
| Internet Archive | Scans | Public domain (jurisdiction-dependent) | One scan already pinned | earlier pin |
| Zenodo | Slide decks, reports | Record licence, cross-checked against the document | One IPBES report rejected: its PDF forbids commercial use despite a CC BY record | 2026-10-03 |
| Language Science Press | Long monograph | CC BY 4.0 on record and imprint page | 100+ pages | 2026-10-03 |

## Extending the sample

- New article from a `core` venue: pick per the plan's article rules, confirm CC BY on the article
  page, add to `sources.json`.
- Promote a `reserve` venue: verify its licence, set `Usable` and `Verified`.
- New field: add a section with three `core` venues and at least one `reserve`.
- A venue that drops CC BY moves to `excluded` with a note; its already-pinned CC BY articles stay
  valid.
