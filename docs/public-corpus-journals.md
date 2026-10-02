# Public corpus journal register

Register of candidate venues for the S3b public PDF corpus
([plan](public-corpus-plan.md), [#115](https://github.com/matthiaskloft/zotero-fulltext-mcp/issues/115)).
It is kept separate from `sources.json` so the sample can be extended later: add articles from a
`core` venue, promote a `reserve` venue, or add rows for a new field.

## Status of this list

Compiled from prior knowledge of the journals' open-access models, **not yet verified against
the journals' current pages**. The licence column is the journal's usual default; individual
articles can differ (older articles, funder-mandated variants, hybrid history). Every article's
licence is verified on its own page before it is pinned, as the plan requires. Update the
`Verified` column when a journal's model has been checked.

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
| PLOS Medicine | core | gold | CC BY 4.0 | Two-column print PDF, structured abstract, tables | — |
| BMC Medicine | core | gold | CC BY 4.0 | Single-column, long methods, supplementary tables | — |
| BMJ Open | core | gold | CC BY-NC 4.0 (**NC**; some CC BY) | Strengths/limitations box, two-column | — |
| eClinicalMedicine | reserve | gold | CC BY / CC BY-NC-ND (**varies**) | Lancet house style | — |
| Trials | reserve | gold | CC BY 4.0 | Protocols, SPIRIT tables | — |

## Physics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Physical Review X | core | gold | CC BY 4.0 | REVTeX two-column, dense equations | — |
| New Journal of Physics | core | gold | CC BY 4.0 | IOP single-column, equations, figures | — |
| SciPost Physics | core | diamond | CC BY 4.0 | Single-column LaTeX, long derivations | — |
| Physical Review Research | reserve | gold | CC BY 4.0 | REVTeX two-column | — |
| European Physical Journal C | reserve | gold (SCOAP3) | CC BY 4.0 | Large-collaboration author lists | — |

## Chemistry

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Chemical Science | core | diamond | CC BY 3.0 / CC BY-NC 3.0 (**varies**) | Reaction schemes, two-column | — |
| Beilstein Journal of Organic Chemistry | core | diamond | CC BY 4.0 | Schemes, compound numbering, spectra | — |
| Journal of Cheminformatics | core | gold | CC BY 4.0 | Single-column, code and tables | — |
| RSC Advances | reserve | gold | CC BY 3.0 / CC BY-NC 3.0 (**varies**) | Two-column | — |
| ACS Central Science | reserve | gold | CC BY / CC BY-NC-ND (**varies**) | ACS two-column | — |

## Ecology and biology

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| eLife | core | gold | CC BY 4.0 | Figure-heavy, long methods, figure supplements | — |
| PLOS Biology | core | gold | CC BY 4.0 | Two-column, multi-panel figures | — |
| Ecology and Evolution | core | gold | CC BY 4.0 | Wiley single-column, species names | — |
| BMC Biology | reserve | gold | CC BY 4.0 | Single-column | — |
| PeerJ | reserve | gold | CC BY 4.0 | Single-column, line numbers in some PDFs | — |

## Statistics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Journal of Statistical Software | core | diamond | CC BY 3.0/4.0 | Code listings, R output | — |
| Electronic Journal of Statistics | core | diamond | CC BY 4.0 | Theorems, proofs, notation | — |
| Bayesian Analysis | core | diamond | CC BY 4.0 | Equations, MCMC figures, discussion papers | — |
| Journal of Machine Learning Research | reserve (already pinned ×2) | diamond | CC BY 4.0 | Long single-column | — |
| Observational Studies | reserve | diamond | CC BY | Causal-inference prose | — |

## Psychometrics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Psych (MDPI) | core | gold | CC BY 4.0 | MDPI template, IRT equations | — |
| Large-scale Assessments in Education | core | gold | CC BY 4.0 | PISA/TIMSS methodology, tables | — |
| Journal of Intelligence (MDPI) | core | gold | CC BY 4.0 | Factor models, path diagrams | — |
| Practical Assessment, Research & Evaluation | reserve | diamond | CC BY-NC (**NC**, check) | Short tutorials | — |
| Measurement Instruments for the Social Sciences | reserve | gold | CC BY 4.0 | Scale documentation, item tables | — |

## Computer science

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Transactions of the ACL (TACL) | core | diamond | CC BY 4.0 | ACL two-column, examples, tables | — |
| LIPIcs (conference proceedings) | core | diamond | CC BY 4.0 | Theory papers, lemmas, pseudocode | — |
| Journal of Artificial Intelligence Research | core | diamond | check per article | Long single-column | — |
| Transactions on Machine Learning Research | reserve | diamond | CC BY 4.0 | OpenReview format | — |
| PeerJ Computer Science | reserve | gold | CC BY 4.0 | Single-column | — |

## Economics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Theoretical Economics | core | diamond | CC BY-NC 4.0 (**NC**) | Proofs, long appendices | — |
| Quantitative Economics | core | diamond | CC BY-NC 4.0 (**NC**) | Regression tables, estimation | — |
| Economics (De Gruyter, formerly E-Journal) | core | gold | CC BY 4.0 | Working-paper style | — |
| IZA Journal of Labor Economics | reserve | gold | CC BY 4.0 | Wide regression tables | — |
| Journal of Economic Structures | reserve | gold | CC BY 4.0 | Input-output tables | — |

## Materials and engineering

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Materials & Design | core | gold | CC BY 4.0 | Elsevier two-column, micrographs, units | — |
| npj Computational Materials | core | gold | CC BY 4.0 | Nature style, simulation figures | — |
| IEEE Access | core | gold | CC BY 4.0 | IEEE two-column, block diagrams | — |
| Advanced Science | reserve | gold | CC BY 4.0 | Wiley, image-heavy | — |
| Scientific Reports | reserve | gold | CC BY 4.0 | Multidisciplinary | — |

## Psychology

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Collabra: Psychology | core | gold | CC BY 4.0 | APA-like, preregistration notes | — |
| Frontiers in Psychology | core | gold | CC BY 4.0 | Frontiers two-column | — |
| Meta-Psychology | core | diamond | CC BY 4.0 | Single-column, reproducibility reports | — |
| International Review of Social Psychology | reserve | diamond | CC BY 4.0 | Ubiquity Press template | — |
| Journal of Open Psychology Data | reserve | gold | CC BY 4.0 | Short data papers | — |

## Education

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| NEPS Survey Papers | core | series | check per paper | Survey documentation, codebook tables; some German | — |
| AERA Open | core | gold | CC BY / CC BY-NC (**varies**) | SAGE single-column | — |
| Frontiers in Education | core | gold | CC BY 4.0 | Frontiers two-column | — |
| Journal for Educational Research Online (JERO) | reserve | diamond | check per article | German/English | — |
| Education Sciences (MDPI) | reserve | gold | CC BY 4.0 | MDPI template | — |

## Sociology and political science

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Sociological Science | core | diamond | CC BY 4.0 | Single-column, regression tables | — |
| Socius | core | gold | CC BY-NC 4.0 (**NC**) | SAGE, data visualizations | — |
| Research & Politics | core | gold | CC BY-NC 4.0 (**NC**) | Short articles | — |
| Politics and Governance | reserve | diamond | CC BY 4.0 | Cogitatio template | — |
| Social Inclusion | reserve | diamond | CC BY 4.0 | Cogitatio template | — |

## Law

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| German Law Journal | core | gold | CC BY 4.0 | Footnote-heavy, case citations | — |
| Utrecht Law Review | core | diamond | CC BY 4.0 | Single-column, footnotes | — |
| Laws (MDPI) | core | gold | CC BY 4.0 | MDPI template, legal citations | — |
| European Journal of Legal Studies | reserve | diamond | check per article | Long footnotes | — |
| Erasmus Law Review | reserve | gold | check per article | Single-column | — |

## Humanities and linguistics

| Journal | Role | Model | Usual licence | Layout | Verified |
| --- | --- | --- | --- | --- | --- |
| Glossa | core | diamond | CC BY 4.0 | Interlinear glosses, numbered examples | — |
| Open Library of Humanities journal | core | diamond | CC BY 4.0 | Long prose, images | — |
| Digital Humanities Quarterly | core | diamond | CC BY-ND (**ND**) | Web-first, generated PDFs | — |
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
