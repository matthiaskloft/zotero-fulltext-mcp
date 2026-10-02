# Public corpus article candidates

Article candidates for the S3b corpus ([plan](public-corpus-plan.md),
[venue register](public-corpus-journals.md)). Two per `core` journal, chosen for popularity
(citations) and, within each field, varied layout and topic.

## How candidates were chosen and what is still unchecked

- Selected 2026-10-02 by web search for each journal's most-cited articles, supplemented by
  well-known highly cited papers where search returned no list. Citation counts quoted are from
  search-result snippets and are only indicative.
- Publisher sites, PubMed Central, DOAJ, Crossref and OpenAlex were not reachable from the
  selection environment, so **no article page has been opened yet**. DOIs are from memory or search
  snippets and must be confirmed at download.
- **Licence status:**
  - `journal CC BY` — the venue applies CC BY to every article in this date range; still check the
    licence line on the PDF at download.
  - `confirm CC BY` — the venue lets authors choose or changed licence; the article's own page must
    show CC BY, CC0 or public domain, otherwise replace it with the next candidate.
- An article enters `sources.json` only after download, SHA-256 pinning and licence confirmation.

## Clinical medicine

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| PLOS Medicine | Ioannidis, *Why Most Published Research Findings Are False* | 2005 | 10.1371/journal.pmed.0020124 | Highly cited essay; prose, few tables | journal CC BY |
| PLOS Medicine | Moher et al., *Preferred Reporting Items for Systematic Reviews and Meta-Analyses: The PRISMA Statement* | 2009 | 10.1371/journal.pmed.1000097 | Most cited PLOS Medicine article; checklist table, flow diagram | journal CC BY |
| npj Digital Medicine | Rieke et al., *The future of digital health with federated learning* | 2020 | 10.1038/s41746-020-00323-1 | Most cited in journal (~3.7k) | confirm CC BY |
| npj Digital Medicine | Sutton et al., *An overview of clinical decision support systems: benefits, risks, and strategies for success* | 2020 | 10.1038/s41746-020-0221-y | Second most cited; review with tables | confirm CC BY |
| Journal of Medical Internet Research | Eysenbach, *Improving the Quality of Web Surveys: The Checklist for Reporting Results of Internet E-Surveys (CHERRIES)* | 2004 | 10.2196/jmir.6.3.e34 | Most cited JMIR article; short, checklist table | confirm CC BY (early volume) |
| Journal of Medical Internet Research | Eysenbach, *The Law of Attrition* | 2005 | 10.2196/jmir.7.1.e11 | Second most cited; essay with figures | confirm CC BY (early volume) |

## Physics

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Physical Review X | Weng et al., *Weyl Semimetal Phase in Noncentrosymmetric Transition-Metal Monophosphides* | 2015 | 10.1103/PhysRevX.5.011029 | Highly cited; band-structure figures, equations | journal CC BY |
| Physical Review X | O'Malley et al., *Scalable Quantum Simulation of Molecular Energies* | 2016 | 10.1103/PhysRevX.6.031007 | Highly cited; quantum circuits, long author list | journal CC BY |
| Advanced Photonics | Galiffi et al., *Photonics of time-varying media* | 2022 | 10.1117/1.AP.4.1.014002 | Most cited review in journal | journal CC BY |
| Advanced Photonics | *Taking silicon photonics modulators to a higher performance level: state-of-the-art and a review of new technologies* | 2021 | confirm | Highly cited review; device figures, tables | journal CC BY |
| Journal of High Energy Physics | Almheiri et al., *Replica wormholes and the entropy of Hawking radiation* | 2020 | 10.1007/JHEP05(2020)013 | Most cited recent JHEP paper; long derivations | journal CC BY (SCOAP3 era) |
| Journal of High Energy Physics | Esteban et al., *The fate of hints: updated global analysis of three-flavor neutrino oscillations* (NuFIT 5.0) | 2020 | 10.1007/JHEP09(2020)178 | Highly cited; fit tables, contour plots | journal CC BY (SCOAP3 era) |

Older most-cited JHEP papers (PYTHIA 6.4 manual, anti-kt algorithm) predate SCOAP3 and are not
CC BY, so they are excluded.

## Chemistry

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| JACS Au | Rorrer et al., *Conversion of Polyolefin Waste to Liquid Alkanes with Ru-Based Catalysts under Mild Conditions* | 2021 | confirm | Most cited in journal | confirm CC BY |
| JACS Au | *Describing Chemical Reactivity with Frontier Molecular Orbitalets* | 2022 | confirm | Highly cited; computational, orbital figures | confirm CC BY |
| Chemical Science | *Surface-enhanced Raman spectroscopy: benefits, trade-offs and future developments* | 2020 | confirm | Highly cited (~860) review; spectra | confirm CC BY |
| Chemical Science | *Electro-organic synthesis – a 21st century technique* | 2020 | confirm | Highly cited (~640) review; reaction schemes | confirm CC BY |
| Beilstein J. Org. Chem. | Baumann & Baxendale, *An overview of the synthetic routes to the best selling drugs containing 6-membered heterocycles* | 2013 | confirm | Highly cited (~410) review; many schemes | journal CC BY |
| Beilstein J. Org. Chem. | Rueping & Nachtsheim, *A review of new developments in the Friedel–Crafts alkylation – From green chemistry to asymmetric catalysis* | 2010 | confirm | Highly cited (~390) review; schemes, tables | journal CC BY |

## Ecology and biology

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Nature Communications | Zhou et al., *Metascape provides a biologist-oriented resource for the analysis of systems-level datasets* | 2019 | 10.1038/s41467-019-09234-6 | Among the journal's most cited; software, figures | confirm CC BY |
| Nature Communications | Ma et al., *Segment anything in medical images* | 2024 | confirm | Highly cited (~2k); ML figures, tables | confirm CC BY |
| eLife | Regev et al., *The Human Cell Atlas* | 2017 | 10.7554/eLife.27041 | One of eLife's most cited (feature article); prose | journal CC BY |
| eLife | Zivanov et al., *New tools for automated high-resolution cryo-EM structure determination in RELION-3* | 2018 | 10.7554/eLife.42166 | Highly cited methods paper; equations, figures | journal CC BY |
| PLOS Biology | Percie du Sert et al., *The ARRIVE guidelines 2.0: Updated guidelines for reporting animal research* | 2020 | 10.1371/journal.pbio.3000410 | Highly cited guideline; tables | journal CC BY |
| PLOS Biology | Sender, Fuchs & Milo, *Revised Estimates for the Number of Human and Bacteria Cells in the Body* | 2016 | 10.1371/journal.pbio.1002533 | Highly cited; estimates, tables, short | journal CC BY |

## Statistics

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Journal of Statistical Software | Bates et al., *Fitting Linear Mixed-Effects Models Using lme4* | 2015 | 10.18637/jss.v067.i01 | Among the most cited statistics papers; code listings, R output | journal CC BY |
| Journal of Statistical Software | Carpenter et al., *Stan: A Probabilistic Programming Language* | 2017 | 10.18637/jss.v076.i01 | Highly cited; code, grammar notation | journal CC BY |
| Electronic Journal of Statistics | Rothman et al., *Sparse permutation invariant covariance estimation* | 2008 | confirm | Most cited in journal (~1.1k); theorems, proofs | journal CC BY (confirm for 2008) |
| Electronic Journal of Statistics | Ravikumar et al., *High-dimensional covariance estimation by minimizing ℓ1-penalized log-determinant divergence* | 2011 | confirm | Second most cited (~900); dense notation | journal CC BY (confirm for 2011) |
| Bayesian Analysis | Gelman, *Prior distributions for variance parameters in hierarchical models* | 2006 | 10.1214/06-BA117A | Classic, highly cited; equations, plots | confirm CC BY (early volume) |
| Bayesian Analysis | Vehtari et al., *Rank-Normalization, Folding, and Localization: An Improved R̂ for Assessing Convergence of MCMC* | 2021 | 10.1214/20-BA1221 | Highly cited; MCMC diagnostics figures | journal CC BY |

## Psychometrics

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Psych (MDPI) | Christensen & Golino, *Estimating the Stability of Psychological Dimensions via Bootstrap Exploratory Graph Analysis: A Monte Carlo Simulation and Tutorial* | 2021 | confirm | Most cited in journal; simulation tables, R code | journal CC BY |
| Psych (MDPI) | Allsop et al., *Qualitative Methods with Nvivo Software: A Practical Guide for Analyzing Qualitative Data* | 2022 | confirm | Second most cited; screenshots, contrast to quantitative papers | journal CC BY |
| Large-scale Assessments in Education | Hernández-Torrano & Courtney, *Modern international large-scale assessment in education: an integrative review and mapping of the literature* | 2021 | 10.1186/s40536-021-00109-1 | Highly cited review; bibliometric maps | journal CC BY |
| Large-scale Assessments in Education | to select | — | — | No second citation ranking found | journal CC BY |
| Psychometrika | Sijtsma, *On the Use, the Misuse, and the Very Limited Usefulness of Cronbach's Alpha* (2025 version) | 2025 | confirm | Most read 2025 article; open-access era | confirm CC BY |
| Psychometrika | Epskamp, *Psychometric Network Models from Time-Series and Panel Data* | 2025 | confirm | Most read 2025; matrix notation, network figures | confirm CC BY |

## Computer science

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| TACL | Bojanowski et al., *Enriching Word Vectors with Subword Information* | 2017 | 10.1162/tacl_a_00051 | Among the most cited NLP papers; ACL two-column | journal CC BY |
| TACL | Liu et al., *Lost in the Middle: How Language Models Use Long Contexts* | 2024 | 10.1162/tacl_a_00638 | Highly cited; plots, retrieval topic close to this project | journal CC BY |
| LIPIcs | to select ×2 | — | — | No citation ranking found; pick from ICALP/SoCG/ITCS volumes | journal CC BY |
| Journal of Artificial Intelligence Research | to select ×2 (May 2023 or later) | — | — | No citation ranking found for CC BY era | confirm CC BY |

## Economics

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Economics (De Gruyter) | to select ×2 | — | — | No citation ranking found | journal CC BY |
| IZA Journal of Labor Economics | *The impact of parental income and education on the schooling of their children* | confirm | confirm | Most cited in journal (~280); regression tables | journal CC BY |
| IZA Journal of Labor Economics | *Feeling useless: the effect of unemployment on mental health in the Great Recession* | confirm | confirm | Highly cited (~140) | journal CC BY |
| Journal for Labour Market Research | *The "Task Approach" to Labor Markets: An Overview* | 2013 | confirm | Most cited in journal (~290) | confirm CC BY (pre-2016 OA switch) |
| Journal for Labour Market Research | *The IAB Establishment Panel—methodological essentials and data quality* | confirm | confirm | Survey methodology; tables | confirm CC BY |

## Materials and engineering

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Science and Technology of Advanced Materials | Kamiya, Nomura & Hosono, *Present status of amorphous In–Ga–Zn–O thin-film transistors* | 2010 | confirm | Highly cited review; device figures | confirm CC BY |
| Science and Technology of Advanced Materials | *Exploration of new superconductors and functional materials, and fabrication of superconducting tapes and wires of iron pnictides* | confirm | confirm | Highly read review; tables of compounds | confirm CC BY |
| Materials & Design | to select ×2 | — | — | No citation ranking found | confirm CC BY |
| npj Computational Materials | Kirklin et al., *The Open Quantum Materials Database (OQMD): assessing the accuracy of DFT formation energies* | 2015 | 10.1038/npjcompumats.2015.10 | Most cited in journal (~2.1k) | confirm CC BY |
| npj Computational Materials | Schmidt et al., *Recent advances and applications of machine learning in solid-state materials science* | 2019 | 10.1038/s41524-019-0221-0 | Second most cited (~2.1k); long review | confirm CC BY |

## Psychology

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Collabra: Psychology | Lakens, *Sample Size Justification* | 2022 | 10.1525/collabra.33267 | Highly cited methods paper; tables, figures | journal CC BY |
| Collabra: Psychology | to select | — | — | — | journal CC BY |
| Frontiers in Psychology | Lakens, *Calculating and reporting effect sizes to facilitate cumulative science: a practical primer for t-tests and ANOVAs* | 2013 | 10.3389/fpsyg.2013.00863 | Very highly cited; formulas, tables | journal CC BY |
| Frontiers in Psychology | to select | — | — | Pick a different author and topic | journal CC BY |
| Meta-Psychology | to select ×2 | — | — | No citation ranking found | journal CC BY |

## Education

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Int. J. of Educational Technology in Higher Education | *Flipped classrooms in higher education during the COVID-19 pandemic: findings and future research recommendations* | 2022 | confirm | Most cited recent article (~210) | journal CC BY |
| Int. J. of Educational Technology in Higher Education | *Digital higher education: a divider or bridge builder? Leadership perspectives on edtech in a COVID-19 reality* | 2021 | confirm | Highly cited (~200); qualitative | journal CC BY |
| Smart Learning Environments | Tlili et al., *What if the devil is my guardian angel: ChatGPT as a case study of using chatbots in education* | 2023 | confirm | Most cited in journal (~880) | journal CC BY |
| Smart Learning Environments | *Exploring blockchain technology and its potential applications for education* | 2018 | confirm | Highly cited (~640) | journal CC BY |
| Int. J. of STEM Education | Kelley & Knowles, *A conceptual framework for integrated STEM education* | 2016 | 10.1186/s40594-016-0046-z | Most cited in journal | confirm CC BY |
| Int. J. of STEM Education | *STEM education K-12: perspectives on integration* | 2016 | confirm | Highly cited (~265) | confirm CC BY |

## Sociology and political science

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Sociological Science | to select ×2 | — | — | No citation ranking found | journal CC BY |
| Politics and Governance | Howlett & Rayner, *Patching vs Packaging in Policy Formulation: Assessing Policy Portfolio Design* | 2013 | confirm | Most cited in journal (~230) | journal CC BY |
| Politics and Governance | Lührmann, Tannenberg & Lindberg, *Regimes of the World (RoW): Opening New Avenues for the Comparative Study of Political Regimes* | 2018 | 10.17645/pag.v6i1.1214 | Highly cited (~190); classification tables | journal CC BY |
| British Journal of Political Science | Valgarðsson et al., *A Crisis of Political Trust? Global Trends in Institutional Trust from 1958 to 2019* | 2025 | confirm | Most read; open-access era (vol. 55+) | confirm CC BY |
| British Journal of Political Science | Bolet & Foos, *Media Platforming and the Normalisation of Extreme Right Views* | 2025 | confirm | Highly read; experiment, regression tables | confirm CC BY |

## Law

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| German Law Journal | to select ×2 (2019 or later) | — | — | Ranking page exists but was not readable | confirm CC BY |
| Utrecht Law Review | to select ×2 | — | — | No citation ranking found | journal CC BY |
| Laws (MDPI) | to select ×2 | — | — | Search returned no usable ranking | journal CC BY |

## Humanities and linguistics

| Journal | Article | Year | DOI (confirm) | Why | Licence status |
| --- | --- | --- | --- | --- | --- |
| Glossa | Pfau, Salzmann & Steinbach, *The syntax of sign language agreement: Common ingredients, but unusual recipe* | 2018 | confirm | One of the most cited (~110); glosses, examples | journal CC BY |
| Glossa | *The strength of the phylogenetic signal in syntactic data* | 2024 | confirm | Recent, highly cited; trees, statistics | journal CC BY |
| Semantics and Pragmatics | Roberts, *Information Structure in Discourse: Towards an Integrated Formal Theory of Pragmatics* | 2012 | 10.3765/sp.5.6 | Most cited (~1.5k); long, formal notation | journal CC BY |
| Semantics and Pragmatics | *Varieties of conventional implicature* | 2010 | confirm | Second most cited (~300) | journal CC BY |
| Journal of Open Humanities Data | *Accessibility, Discoverability, and Functionality: An Audit of and Recommendations for Digital Language Archives* | confirm | confirm | Most cited in journal; short data paper | journal CC BY |
| Journal of Open Humanities Data | *The CONLIT Dataset of Contemporary Literature* | confirm | confirm | Second most cited; dataset description | journal CC BY |

## Status

| | Count |
| --- | ---: |
| Candidates named | 63 |
| Slots still `to select` | 21 |
| Target (42 journals × 2) | 84 |

The remaining slots need a source that ranks articles within those journals (journal "most cited"
pages, Google Scholar, OpenAlex). These were not reachable from the selection environment;
picking from them on the host, or allowing those hosts, is the next step.
