# Public corpus journal register

Register of candidate venues for the S3b public PDF corpus
([plan](public-corpus-plan.md), [#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115)).
It is kept separate from `sources.json` so the sample can be extended later: add articles from a
`core` venue, promote a `reserve` venue, or add rows for a new field.

## Status of this list

The 42 `core` journals were checked on 2026-10-02 by web search against journal, publisher and
DOAJ pages (DOAJ and some publisher sites could not be fetched directly from the checking
environment, so some entries rest on search-result excerpts of those pages). The exception is
NEPS Survey Papers: no licence statement was found, so it stays unverified. `reserve` venues,
non-English venues and extra-format sources are not yet verified.

Several journals let authors choose between licences (marked **varies**); some changed licence
over time (dates noted). Every article's licence is still verified on its own page before it is
pinned, as the plan requires.

Columns:

- **Role** — `core`: used for the first sample (3 per field). `reserve`: replacement or
  extension candidate.
- **Model** — `gold`: fully open access, no subscription content. `diamond`: open access with no
  author fees. `series`: report/working-paper series. `platform`: aggregator or repository.
- **Usual licence** — expected default; NC/ND/SA variants flagged because they restrict derived
  fixtures (metadata-only pinning is still fine).
- **Layout** — what the venue adds to layout diversity.
- **Verified** — date the OA model and licence were checked on the journal site, or `—`.

## Clinical medicine

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| PLOS Medicine | core | gold | CC BY 4.0 | Two-column print PDF, structured abstract, tables | 2026-10-02 |
| BMC Medicine | core | gold | CC BY 4.0 or CC BY-NC-ND 4.0 (**varies**, author choice) | Single-column, long methods, supplementary tables | 2026-10-02 |
| BMJ Open | core | gold | CC BY-NC 4.0 default (**NC**); CC BY where funders mandate it | Strengths/limitations box, two-column | 2026-10-02 |
| eClinicalMedicine | reserve | gold | CC BY / CC BY-NC-ND (**varies**) | Lancet house style | — |
| Trials | reserve | gold | CC BY 4.0 | Protocols, SPIRIT tables | — |

## Physics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Physical Review X | core | gold | CC BY 4.0 | REVTeX two-column, dense equations | 2026-10-02 |
| New Journal of Physics | core | gold | CC BY 3.0 since Nov 2012; earlier CC BY-NC-SA 3.0 (**NC/SA**) | IOP single-column, equations, figures | 2026-10-02 |
| SciPost Physics | core | diamond | CC BY 4.0 | Single-column LaTeX, long derivations | 2026-10-02 |
| Physical Review Research | reserve | gold | CC BY 4.0 | REVTeX two-column | — |
| European Physical Journal C | reserve | gold (SCOAP3) | CC BY 4.0 | Large-collaboration author lists | — |

## Chemistry

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Chemical Science | core | gold (diamond until Jun 2026; APC from 1 Jul 2026) | CC BY 3.0 or CC BY-NC 3.0 (**varies**) | Reaction schemes, two-column | 2026-10-02 |
| Beilstein Journal of Organic Chemistry | core | diamond | CC BY 4.0 | Schemes, compound numbering, spectra | 2026-10-02 |
| Journal of Cheminformatics | core | gold | CC BY 4.0 or CC BY-NC-ND 4.0 (**varies**, author choice) | Single-column, code and tables | 2026-10-02 |
| RSC Advances | reserve | gold | CC BY 3.0 / CC BY-NC 3.0 (**varies**) | Two-column | — |
| ACS Central Science | reserve | gold | CC BY / CC BY-NC-ND (**varies**) | ACS two-column | — |

## Ecology and biology

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| eLife | core | gold | CC BY 4.0 (some older CC BY 3.0, CC0) | Figure-heavy, long methods, figure supplements | 2026-10-02 |
| PLOS Biology | core | gold | CC BY 4.0 | Two-column, multi-panel figures | 2026-10-02 |
| Ecology and Evolution | core | gold | CC BY since Aug 2012; earlier CC BY-NC (**NC**) | Wiley single-column, species names | 2026-10-02 |
| BMC Biology | reserve | gold | CC BY 4.0 | Single-column | — |
| PeerJ | reserve | gold | CC BY 4.0 | Single-column, line numbers in some PDFs | — |

## Statistics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Journal of Statistical Software | core | diamond | CC BY (version per article) | Code listings, R output | 2026-10-02 |
| Electronic Journal of Statistics | core | diamond | CC BY 4.0 | Theorems, proofs, notation | 2026-10-02 |
| Bayesian Analysis | core | diamond | CC BY 4.0 | Equations, MCMC figures, discussion papers | 2026-10-02 |
| Journal of Machine Learning Research | reserve (already pinned ×2) | diamond | CC BY 4.0 | Long single-column | — |
| Observational Studies | reserve | diamond | CC BY | Causal-inference prose | — |

## Psychometrics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Psych (MDPI) | core | gold | CC BY 4.0 | MDPI template, IRT equations | 2026-10-02 |
| Large-scale Assessments in Education | core | diamond | CC BY 4.0 | PISA/TIMSS methodology, tables | 2026-10-02 |
| Journal of Intelligence (MDPI) | core | gold | CC BY 4.0 | Factor models, path diagrams | 2026-10-02 |
| Practical Assessment, Research & Evaluation | reserve | diamond | CC BY-NC (**NC**, check) | Short tutorials | — |
| Measurement Instruments for the Social Sciences | reserve | gold | CC BY 4.0 | Scale documentation, item tables | — |

## Computer science

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Transactions of the ACL (TACL) | core | diamond | CC BY 4.0 | ACL two-column, examples, tables | 2026-10-02 |
| LIPIcs (conference proceedings) | core | diamond | CC BY 4.0 | Theory papers, lemmas, pseudocode | 2026-10-02 |
| Journal of Artificial Intelligence Research | core | diamond | CC BY since May 2023 (vol. 77+); earlier JAIR License v1 (**not CC**) | Long single-column | 2026-10-02 |
| Transactions on Machine Learning Research | reserve | diamond | CC BY 4.0 | OpenReview format | — |
| PeerJ Computer Science | reserve | gold | CC BY 4.0 | Single-column | — |

## Economics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Theoretical Economics | core | diamond | CC BY-NC (**NC**) | Proofs, long appendices | 2026-10-02 |
| Quantitative Economics | core | diamond | CC BY-NC (**NC**) | Regression tables, estimation | 2026-10-02 |
| Economics (De Gruyter, formerly E-Journal) | core | gold | CC BY 4.0 | Working-paper style | 2026-10-02 |
| IZA Journal of Labor Economics | reserve | gold | CC BY 4.0 | Wide regression tables | — |
| Journal of Economic Structures | reserve | gold | CC BY 4.0 | Input-output tables | — |

## Materials and engineering

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Materials & Design | core | gold | CC BY, CC BY-NC or CC BY-NC-ND (**varies**) | Elsevier two-column, micrographs, units | 2026-10-02 |
| npj Computational Materials | core | gold | CC BY 4.0 or CC BY-NC-ND 4.0 (**varies**) | Nature style, simulation figures | 2026-10-02 |
| IEEE Access | core | gold | CC BY 4.0 or CC BY-NC-ND 4.0 (**varies**) | IEEE two-column, block diagrams | 2026-10-02 |
| Advanced Science | reserve | gold | CC BY 4.0 | Wiley, image-heavy | — |
| Scientific Reports | reserve | gold | CC BY 4.0 | Multidisciplinary | — |

## Psychology

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Collabra: Psychology | core | gold | CC BY 4.0 | APA-like, preregistration notes | 2026-10-02 |
| Frontiers in Psychology | core | gold | CC BY 4.0 | Frontiers two-column | 2026-10-02 |
| Meta-Psychology | core | diamond | CC BY | Single-column, reproducibility reports | 2026-10-02 |
| International Review of Social Psychology | reserve | diamond | CC BY 4.0 | Ubiquity Press template | — |
| Journal of Open Psychology Data | reserve | gold | CC BY 4.0 | Short data papers | — |

## Education

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| NEPS Survey Papers | core | series | check per paper | Survey documentation, codebook tables; some German | — |
| AERA Open | core | gold | CC BY-NC default (**NC**) | SAGE single-column | 2026-10-02 |
| Frontiers in Education | core | gold | CC BY 4.0 | Frontiers two-column | 2026-10-02 |
| Journal for Educational Research Online (JERO) | reserve | diamond | check per article | German/English | — |
| Education Sciences (MDPI) | reserve | gold | CC BY 4.0 | MDPI template | — |

## Sociology and political science

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Sociological Science | core | gold (publication fee) | CC BY | Single-column, regression tables | 2026-10-02 |
| Socius | core | gold | CC BY or CC BY-NC (**varies**) | SAGE, data visualizations | 2026-10-02 |
| Research & Politics | core | gold | CC BY-NC mostly (**NC**); also CC BY, CC BY-NC-ND | Short articles | 2026-10-02 |
| Politics and Governance | reserve | diamond | CC BY 4.0 | Cogitatio template | — |
| Social Inclusion | reserve | diamond | CC BY 4.0 | Cogitatio template | — |

## Law

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| German Law Journal | core | diamond | CC BY 4.0 since 2019 | Footnote-heavy, case citations | 2026-10-02 |
| Utrecht Law Review | core | diamond | CC BY 4.0 | Single-column, footnotes | 2026-10-02 |
| Laws (MDPI) | core | diamond (no APC) | CC BY 4.0 | MDPI template, legal citations | 2026-10-02 |
| European Journal of Legal Studies | reserve | diamond | check per article | Long footnotes | — |
| Erasmus Law Review | reserve | gold | check per article | Single-column | — |

## Humanities and linguistics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Glossa | core | diamond | CC BY 4.0 | Interlinear glosses, numbered examples | 2026-10-02 |
| Open Library of Humanities journal | core | diamond | CC BY 4.0 default | Long prose, images | 2026-10-02 |
| Digital Humanities Quarterly | core | diamond | CC BY-ND 4.0 default (**ND**); authors may choose CC BY or CC0 | Web-first, generated PDFs | 2026-10-02 |
| Journal of Open Humanities Data | reserve | gold | CC BY 4.0 | Short data papers | — |
| Language Science Press (books) | reserve | diamond | CC BY 4.0 | Monographs; also the long-book candidate | — |

## Non-English venues

| Venue | Language | Role | Model | Usual licence | Field | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| Zeitschrift für Soziologie | de | core | gold | CC BY 4.0 | Sociology | — |
| Forum Qualitative Sozialforschung (FQS) | de (multilingual) | core | diamond | CC BY 4.0 | Social science methods | — |
| OpenEdition Journals (e.g. *Sociologie*, *Formation emploi*) | fr | core | platform | varies by journal | Sociology/education | — |
| Économie et Statistique / Economics and Statistics (INSEE) | fr | core | diamond | check | Economics | — |
| Revista Española de Investigaciones Sociológicas (REIS) | es | core | diamond | check | Sociology | — |
| Comunicar | es | core | gold | CC BY-NC (**NC**) | Education/media | — |
| SciELO (journal collection) | es/pt | reserve | platform | varies by journal | Any | — |

## Extra formats

| Source | Format | Role | Usual licence | Notes | Verified |
| --- | --- | --- | --- | --- | --- |
| Internet Archive / Biodiversity Heritage Library | Scans | core | Public domain (jurisdiction-dependent) | Image-only and poor-OCR items; one scan already pinned | — |
| HathiTrust (full-view public domain) | Scans | reserve | Public domain (US) | Check download terms | — |
| Zenodo | Slide decks, reports | core | per record (prefer CC BY) | Filter by resource type | — |
| NEPS technical reports | Grey literature | core | check per report | Survey methodology | — |
| Language Science Press / OAPEN | Long monograph | core | CC BY 4.0 (OAPEN varies) | 100+ pages | — |

## Extending the sample

- New article from a `core` venue: pick per the plan's article rules, verify the licence on the
  article page, add to `sources.json`.
- Promote a `reserve` venue: change its role, set `Verified` after checking the journal site.
- New field: add a section with three `core` and at least two `reserve` venues, and a row in the
  plan's field table.
- Drop a venue whose model changes (e.g. becomes hybrid); keep the row with a note so it is not
  re-added.
