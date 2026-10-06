# Public corpus articles

Selected articles for the S3b corpus ([plan](public-corpus-plan.md),
[venue register](public-corpus-journals.md)). Every row below is downloaded, pinned by SHA-256 and
page count in `benchmarks/public_pdfs/sources.json`, and has a confirmed licence. Quality labels
(`expected_quality`) are still empty: they come from the maintainer's page review, never from
extractor output.

## How articles were chosen and checked (2026-10-02/03)

- **Selection.** Two per `core` journal. Where the first list named no article, OpenAlex was
  queried by journal with `best_oa_location.license:cc-by` and sorted by `cited_by_count`
  (DataCite for LIPIcs, whose DOIs OpenAlex does not attribute to the series). Within each field,
  topics were kept apart and layouts varied. Named candidates that turned out to be CC BY were kept
  even when they were not the journal's most cited article.
- **Metadata.** DOIs, titles, years and authors come from Crossref or DataCite.
- **Licence.** The article's own statement decides. `Checked on` says where it was read:
  - `article page`: the publisher's landing page, opened in a browser.
  - `PMC licence record`: the publisher page sits behind a bot challenge; the licence element of the
    PubMed Central record, the PMC open-data licence code and Crossref agree.
  - `PDF notice`: the landing page is blocked or shows no licence; the licence line printed in the
    article PDF decides (Crossref agrees where it carries one).
  - `record + PDF`: repository record (Zenodo, NASA NTRS) and the document itself.
- **Download.** Cached under the ignored `benchmarks/public_pdfs/.cache/`, never committed.
  Where the publisher blocks scripted access, the PDF was pinned from a PubMed Central, repository
  or file-server copy of the published version (named in the entry's `note`), or, as a last resort,
  hashed in a browser that had passed the publisher's own check. Bot challenges were not bypassed.
- **Cited by** is OpenAlex `cited_by_count` on 2026-10-03.
- ¹ The publisher refuses scripted downloads; `tools/fetch_public_pdf_corpus.py` reports these and
  continues. Save the PDF from the entry's `url` into the cache and rerun the tool, which verifies
  the checksum.

## Status

| | Count |
| --- | ---: |
| Journal slots (41 journals × 2; Materials & Design dropped, see below) | 82 |
| Filled and pinned | 82 |
| Extras pinned (languages 6, scans 2, report 1, slides 1, monograph 1) | 11 |
| New `sources.json` entries | 93 |
| … of which need a manual browser download ¹ | 26 |
| Pages in new entries | 2961 |

## Articles

### Clinical medicine

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| PLOS Medicine | Ioannidis, *Why Most Published Research Findings Are False* | 2005 | 10.1371/journal.pmed.0020124 | CC BY 4.0 | article page | 10815 | `plosmed-research-findings-false` |
| PLOS Medicine | Moher et al., *Preferred Reporting Items for Systematic Reviews and Meta-Analyses: The PRISMA Statement* | 2009 | 10.1371/journal.pmed.1000097 | CC BY 4.0 | article page | 65288 | `plosmed-prisma-2009` |
| npj Digital Medicine | Rieke et al., *The future of digital health with federated learning* | 2020 | 10.1038/s41746-020-00323-1 | CC BY 4.0 | article page | 3113 | `npjdm-federated-learning` |
| npj Digital Medicine | Sutton et al., *An overview of clinical decision support systems: benefits, risks, and strategies for success* | 2020 | 10.1038/s41746-020-0221-y | CC BY 4.0 | article page | 3118 | `npjdm-clinical-decision-support` |
| JMIR | Eysenbach, *Improving the Quality of Web Surveys: The Checklist for Reporting Results of Internet E-Surveys (CHERRIES)* | 2004 | 10.2196/jmir.6.3.e34 | CC BY 2.0 | article page | 6723 | `jmir-cherries` ¹ |
| JMIR | Eysenbach, *The Law of Attrition* | 2005 | 10.2196/jmir.7.1.e11 | CC BY 2.0 | article page | 2761 | `jmir-law-of-attrition` ¹ |

### Physics

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Physical Review X | Weng et al., *Weyl Semimetal Phase in Noncentrosymmetric Transition-Metal Monophosphides* | 2015 | 10.1103/PhysRevX.5.011029 | CC BY 3.0 | article page | 1451 | `prx-weyl-semimetal` |
| Physical Review X | O’Malley et al., *Scalable Quantum Simulation of Molecular Energies* | 2016 | 10.1103/PhysRevX.6.031007 | CC BY 3.0 | article page | 806 | `prx-quantum-simulation` |
| Advanced Photonics | Rahim et al., *Taking silicon photonics modulators to a higher performance level: state-of-the-art and a review of new technologies* | 2021 | 10.1117/1.AP.3.2.024003 | CC BY 4.0 | PDF notice | 398 | `ap-silicon-modulators` |
| JHEP | Almheiri et al., *Replica wormholes and the entropy of Hawking radiation* | 2020 | 10.1007/JHEP05(2020)013 | CC BY 4.0 | article page | 927 | `jhep-replica-wormholes` ¹ |
| JHEP | Esteban et al., *The fate of hints: updated global analysis of three-flavor neutrino oscillations* | 2020 | 10.1007/JHEP09(2020)178 | CC BY 4.0 | article page | 1177 | `jhep-nufit-5` ¹ |
| Advanced Photonics | Huang et al., *Pushing the limit of high-Q mode of a single dielectric nanocavity* | 2021 | 10.1117/1.AP.3.1.016004 | CC BY 4.0 | PDF notice | 154 | `ap-high-q-nanocavity` |

### Chemistry

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| JACS Au | Grimm et al., *A General Method to Improve Fluorophores Using Deuterated Auxochromes* | 2021 | 10.1021/jacsau.1c00006 | CC BY 4.0 | PMC licence record | 273 | `jacsau-deuterated-fluorophores` |
| JACS Au | Shire, *Conquering the Synthesis and Functionalization of Bicyclo[1.1.1]pentanes* | 2023 | 10.1021/jacsau.3c00014 | CC BY 4.0 | PMC licence record | 182 | `jacsau-bicyclopentanes` |
| Chemical Science | Pérez-Jiménez et al., *Surface-enhanced Raman spectroscopy: benefits, trade-offs and future developments* | 2020 | 10.1039/D0SC00809E | CC BY 3.0 | PMC licence record | 928 | `chemsci-sers-review` |
| Chemical Science | Hoke et al., *Reversible photo-induced trap formation in mixed-halide hybrid perovskites for photovoltaics* | 2015 | 10.1039/C4SC03141E | CC BY 3.0 | PMC licence record | 2312 | `chemsci-perovskite-traps` |
| Beilstein J. Org. Chem. | Baumann, *An overview of the synthetic routes to the best selling drugs containing 6-membered heterocycles* | 2013 | 10.3762/bjoc.9.265 | CC BY 2.0 | article page | 866 | `bjoc-heterocycle-drugs` |
| Beilstein J. Org. Chem. | Rueping, *A review of new developments in the Friedel–Crafts alkylation – From green chemistry to asymmetric catalysis* | 2010 | 10.3762/bjoc.6.6 | CC BY 2.0 | article page | 674 | `bjoc-friedel-crafts` |

### Ecology and biology

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Nature Communications | Zhou et al., *Metascape provides a biologist-oriented resource for the analysis of systems-level datasets* | 2019 | 10.1038/s41467-019-09234-6 | CC BY 4.0 | article page | 16133 | `natcomm-metascape` |
| Nature Communications | Ma et al., *Segment anything in medical images* | 2024 | 10.1038/s41467-024-44824-z | CC BY 4.0 | article page | 2911 | `natcomm-medsam` |
| eLife | Regev et al., *The Human Cell Atlas* | 2017 | 10.7554/eLife.27041 | CC BY 4.0 | article page | 2457 | `elife-human-cell-atlas` |
| eLife | Zivanov et al., *New tools for automated high-resolution cryo-EM structure determination in RELION-3* | 2018 | 10.7554/eLife.42166 | CC BY 4.0 | article page | 5562 | `elife-relion-3` |
| PLOS Biology | Percie du Sert et al., *The ARRIVE guidelines 2.0: Updated guidelines for reporting animal research* | 2020 | 10.1371/journal.pbio.3000410 | CC0 1.0 | article page | 6077 | `plosbio-arrive-2` |
| PLOS Biology | Sender et al., *Revised Estimates for the Number of Human and Bacteria Cells in the Body* | 2016 | 10.1371/journal.pbio.1002533 | CC BY 4.0 | article page | 5304 | `plosbio-cells-in-body` |

### Statistics

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| J. Stat. Softw. | Bates et al., *Fitting Linear Mixed-Effects Models Using lme4* | 2015 | 10.18637/jss.v067.i01 | CC BY 3.0 | article page | 89612 | `jss-lme4` |
| J. Stat. Softw. | Carpenter et al., *Stan: A Probabilistic Programming Language* | 2017 | 10.18637/jss.v076.i01 | CC BY 3.0 | article page | 8373 | `jss-stan` |
| Electron. J. Stat. | Piironen, *Sparsity information and regularization in the horseshoe and other shrinkage priors* | 2017 | 10.1214/17-EJS1337SI | CC BY 4.0 | article page | 487 | `ejs-horseshoe` ¹ |
| Electron. J. Stat. | Kennedy, *Towards optimal doubly robust estimation of heterogeneous causal effects* | 2023 | 10.1214/23-EJS2157 | CC BY 4.0 | article page | 166 | `ejs-doubly-robust` ¹ |
| Bayesian Analysis | Vehtari et al., *Rank-Normalization, Folding, and Localization: An Improved Rˆ for Assessing Convergence of MCMC (with Discussion)* | 2021 | 10.1214/20-BA1221 | CC BY 4.0 | article page | 1708 | `ba-rank-normalized-rhat` ¹ |
| Bayesian Analysis | Letham et al., *Constrained Bayesian Optimization with Noisy Experiments* | 2019 | 10.1214/18-BA1110 | CC BY 4.0 | article page | 318 | `ba-constrained-bayesopt` ¹ |

### Psychometrics

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Psych | Christensen, *Estimating the Stability of Psychological Dimensions via Bootstrap Exploratory Graph Analysis: A Monte Carlo Simulation and Tutorial* | 2021 | 10.3390/psych3030032 | CC BY 4.0 | PDF notice | 320 | `psych-bootstrap-ega` |
| Psych | Allsop et al., *Qualitative Methods with Nvivo Software: A Practical Guide for Analyzing Qualitative Data* | 2022 | 10.3390/psych4020013 | CC BY 4.0 | PDF notice | 300 | `psych-nvivo-guide` |
| Large-scale Assess. Educ. | Hernández-Torrano, *Modern international large-scale assessment in education: an integrative review and mapping of the literature* | 2021 | 10.1186/s40536-021-00109-1 | CC BY 4.0 | article page | 66 | `lsae-ilsa-review` ¹ |
| Large-scale Assess. Educ. | Lorah, *Effect size measures for multilevel models: definition, interpretation, and TIMSS example* | 2018 | 10.1186/s40536-018-0061-2 | CC BY 4.0 | article page | 671 | `lsae-multilevel-effect-sizes` ¹ |
| Psychometrika | McNeish, *SRMR for Models with Covariates* | 2025 | 10.1017/psy.2024.10 | CC BY 4.0 | article page | 11 | `pmet-srmr-covariates` ¹ |
| Psychometrika | Epskamp, *Psychometric Network Models from Time-Series and Panel Data* | 2020 | 10.1007/s11336-020-09697-3 | CC BY 4.0 | article page | 370 | `pmet-network-panel-models` |

### Computer science

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| TACL | Bojanowski et al., *Enriching Word Vectors with Subword Information* | 2017 | 10.1162/tacl_a_00051 | CC BY 4.0 | PDF notice | 9978 | `tacl-fasttext-subwords` |
| TACL | Liu et al., *Lost in the Middle: How Language Models Use Long Contexts* | 2024 | 10.1162/tacl_a_00638 | CC BY 4.0 | PDF notice | 1360 | `tacl-lost-in-the-middle` |
| LIPIcs | Dell et al., *Lovász Meets Weisfeiler and Leman* | 2018 | 10.4230/LIPIcs.ICALP.2018.40 | CC BY 3.0 | article page | 11 | `lipics-lovasz-weisfeiler-leman` |
| LIPIcs | Cohen et al., *Cubical Type Theory: A Constructive Interpretation of the Univalence Axiom* | 2018 | 10.4230/LIPIcs.TYPES.2015.5 | CC BY 3.0 | article page | 175 | `lipics-cubical-type-theory` |
| JAIR | Ponomareva et al., *How to DP-fy ML: A Practical Guide to Machine Learning with Differential Privacy* | 2023 | 10.1613/jair.1.14649 | CC BY 4.0 | PDF notice | 162 | `jair-dp-fy-ml` |
| JAIR | Mandi et al., *Decision-Focused Learning: Foundations, State of the Art, Benchmark and Future Opportunities* | 2024 | 10.1613/jair.1.15320 | CC BY 4.0 | PDF notice | 129 | `jair-decision-focused-learning` |

### Economics

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Economics | Barbier, *The Sustainable Development Goals and the systems approach to sustainability* | 2017 | 10.5018/economics-ejournal.ja.2017-28 | CC BY 4.0 | article page | 471 | `econ-sdg-systems` ¹ |
| IZA J. Labor Econ. | Chevalier et al., *The impact of parental income and education on the schooling of their children* | 2013 | 10.1186/2193-8997-2-8 | CC BY 2.0 | article page | 314 | `izajle-parental-income-schooling` ¹ |
| IZA J. Labor Econ. | Farré et al., *Feeling useless: the effect of unemployment on mental health in the Great Recession* | 2018 | 10.1186/s40172-018-0068-5 | CC BY 4.0 | article page | 131 | `izajle-feeling-useless` ¹ |
| J. Labour Market Res. | Handel, *The O*NET content model: strengths and limitations* | 2016 | 10.1007/s12651-016-0199-8 | CC BY (version not stated) | article page | 135 | `jlmr-onet-content-model` ¹ |
| J. Labour Market Res. | Iwasaki, *Gender wage gap in China: a large meta-analysis* | 2020 | 10.1186/s12651-020-00279-5 | CC BY 4.0 | article page | 82 | `jlmr-gender-wage-gap-china` ¹ |
| Economics | Chetty et al., *Bridging the digital divide: measuring digital literacy* | 2018 | 10.5018/economics-ejournal.ja.2018-23 | CC BY 4.0 | article page | 299 | `econ-digital-literacy` ¹ |

### Materials and engineering

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Sci. Technol. Adv. Mater. | Hosono et al., *Exploration of new superconductors and functional materials, and fabrication of superconducting tapes and wires of iron pnictides* | 2015 | 10.1088/1468-6996/16/3/033503 | CC BY 3.0 | PMC licence record | 317 | `stam-iron-pnictides` |
| Sci. Technol. Adv. Mater. | Yang et al., *Structure of graphene and its disorders: a review* | 2018 | 10.1080/14686996.2018.1494493 | CC BY 4.0 | PMC licence record | 766 | `stam-graphene-disorders` |
| npj Comput. Mater. | Kirklin et al., *The Open Quantum Materials Database (OQMD): assessing the accuracy of DFT formation energies* | 2015 | 10.1038/npjcompumats.2015.10 | CC BY 4.0 | article page | 2468 | `npjcm-oqmd` ¹ |
| npj Comput. Mater. | Schmidt et al., *Recent advances and applications of machine learning in solid-state materials science* | 2019 | 10.1038/s41524-019-0221-0 | CC BY 4.0 | article page | 2672 | `npjcm-ml-solid-state` ¹ |

### Psychology

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Collabra: Psychology | Lakens, *Sample Size Justification* | 2022 | 10.1525/collabra.33267 | CC BY 4.0 | PDF notice | 1757 | `collabra-sample-size` |
| Frontiers in Psychology | Lakens, *Calculating and reporting effect sizes to facilitate cumulative science: a practical primer for t-tests and ANOVAs* | 2013 | 10.3389/fpsyg.2013.00863 | CC BY 3.0 | article page | 10537 | `fpsyg-effect-sizes` |
| Frontiers in Psychology | Laborde et al., *Heart Rate Variability and Cardiac Vagal Tone in Psychophysiological Research – Recommendations for Experiment Planning, Data Analysis, and Data Reporting* | 2017 | 10.3389/fpsyg.2017.00213 | CC BY 4.0 | article page | 2367 | `fpsyg-hrv-recommendations` |
| Meta-Psychology | Van den Akker et al., *Preregistration of secondary data analysis: A template and tutorial* | 2021 | 10.15626/MP.2020.2625 | CC BY 4.0 | article page | 101 | `metapsych-prereg-secondary` |
| Meta-Psychology | Mackinnon et al., *Tutorial in Longitudinal Measurement Invariance and Cross-lagged Panel Models Using Lavaan* | 2022 | 10.15626/MP.2020.2595 | CC BY 4.0 | article page | 85 | `metapsych-lavaan-invariance` |
| Collabra: Psychology | Saunders et al., *Reported Self-control is not Meaningfully Associated with Inhibition-related Executive Function: A Bayesian Analysis* | 2018 | 10.1525/collabra.134 | CC BY 4.0 | PDF notice | 141 | `collabra-self-control-inhibition` |

### Education

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Int. J. Educ. Technol. High. Educ. | Divjak et al., *Flipped classrooms in higher education during the COVID-19 pandemic: findings and future research recommendations* | 2022 | 10.1186/s41239-021-00316-4 | CC BY 4.0 | article page | 222 | `ijethe-flipped-classrooms` ¹ |
| Int. J. Educ. Technol. High. Educ. | Laufer et al., *Digital higher education: a divider or bridge builder? Leadership perspectives on edtech in a COVID-19 reality* | 2021 | 10.1186/s41239-021-00287-6 | CC BY 4.0 | article page | 222 | `ijethe-digital-higher-ed` ¹ |
| Smart Learning Environments | Tlili et al., *What if the devil is my guardian angel: ChatGPT as a case study of using chatbots in education* | 2023 | 10.1186/s40561-023-00237-x | CC BY 4.0 | article page | 1898 | `sle-chatgpt-chatbots` ¹ |
| Smart Learning Environments | Chen et al., *Exploring blockchain technology and its potential applications for education* | 2018 | 10.1186/s40561-017-0050-x | CC BY 4.0 | article page | 751 | `sle-blockchain-education` ¹ |
| Int. J. STEM Educ. | Kelley, *A conceptual framework for integrated STEM education* | 2016 | 10.1186/s40594-016-0046-z | CC BY 4.0 | article page | 1809 | `ijstem-integrated-framework` ¹ |
| Int. J. STEM Educ. | English, *STEM education K-12: perspectives on integration* | 2016 | 10.1186/s40594-016-0036-1 | CC BY 4.0 | article page | 1085 | `ijstem-k12-integration` ¹ |

### Sociology and political science

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Sociological Science | Mize, *Best Practices for Estimating, Interpreting, and Presenting Nonlinear Interaction Effects* | 2019 | 10.15195/v6.a4 | CC BY 4.0 | article page | 981 | `socsci-nonlinear-interactions` |
| Sociological Science | Hout, *Explaining Why More Americans Have No Religious Preference: Political Backlash and Generational Succession, 1987-2012* | 2014 | 10.15195/v1.a24 | CC BY (version not stated; notice in PDF) | PDF notice | 426 | `socsci-no-religious-preference` |
| Politics and Governance | Howlett, *Patching vs Packaging in Policy Formulation: Assessing Policy Portfolio Design* | 2013 | 10.17645/pag.v1i2.95 | CC BY 4.0 (article page; the PDF notice says CC BY 3.0) | article page | 353 | `pag-policy-portfolios` |
| Politics and Governance | Lührmann et al., *Regimes of the World (RoW): Opening New Avenues for the Comparative Study of Political Regimes* | 2018 | 10.17645/pag.v6i1.1214 | CC BY 4.0 | article page | 607 | `pag-regimes-of-the-world` |
| Br. J. Polit. Sci. | Valgarðsson et al., *A Crisis of Political Trust? Global Trends in Institutional Trust from 1958 to 2019* | 2025 | 10.1017/S0007123424000498 | CC BY 4.0 | article page | 80 | `bjpols-political-trust` |
| Br. J. Polit. Sci. | Bolet, *Media Platforming and the Normalisation of Extreme Right Views* | 2025 | 10.1017/S0007123425000195 | CC BY 4.0 | article page | 8 | `bjpols-media-platforming` |

### Law

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| German Law Journal | Lenaerts, *Limits on Limitations: The Essence of Fundamental Rights in the EU* | 2019 | 10.1017/glj.2019.62 | CC BY 4.0 | article page | 94 | `glj-essence-fundamental-rights` |
| German Law Journal | Costello, *Border Justice: Migration and Accountability for Human Rights Violations* | 2020 | 10.1017/glj.2020.27 | CC BY 4.0 | article page | 87 | `glj-border-justice` |
| Utrecht Law Review | Zuiderveen Borgesius et al., *Online Political Microtargeting: Promises and Threats for Democracy* | 2018 | 10.18352/ulr.420 | CC BY 4.0 | article page | 338 | `ulr-political-microtargeting` |
| Utrecht Law Review | Bedner, *Plurality of marriage law and marriage registration for Muslims in Indonesia: a plea for pragmatism* | 2010 | 10.18352/ulr.130 | CC BY 4.0 | article page | 100 | `ulr-indonesian-marriage-law` |
| Laws | Degener, *Disability in a Human Rights Context* | 2016 | 10.3390/laws5030035 | CC BY 4.0 | PDF notice | 356 | `laws-disability-human-rights` |
| Laws | Jacometti, *Circular Economy and Waste in the Fashion Industry* | 2019 | 10.3390/laws8040027 | CC BY 4.0 | PDF notice | 141 | `laws-fashion-circular-economy` |

### Humanities and linguistics

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| Glossa | Pfau et al., *The syntax of sign language agreement: Common ingredients, but unusual recipe* | 2018 | 10.5334/gjgl.511 | CC BY 4.0 | article page | 148 | `glossa-sign-language-agreement` |
| Glossa | Hartmann, *The strength of the phylogenetic signal in syntactic data* | 2024 | 10.16995/glossa.10598 | CC BY 4.0 | article page | 5 | `glossa-phylogenetic-signal` |
| J. Open Humanit. Data | Yi et al., *Accessibility, Discoverability, and Functionality: An Audit of and Recommendations for Digital Language Archives* | 2022 | 10.5334/johd.59 | CC BY 4.0 | article page | 5 | `johd-language-archives-audit` |
| J. Open Humanit. Data | Piper, *The CONLIT Dataset of Contemporary Literature* | 2022 | 10.5334/johd.88 | CC BY 4.0 (article; the described dataset is CC BY-NC 4.0 and is not part of this corpus) | article page | 5 | `johd-conlit-dataset` |
| Semantics and Pragmatics | Ginzburg et al., *Disfluencies as intra-utterance dialogue moves* | 2014 | 10.3765/sp.7.9 | CC BY 3.0 | article page | 180 | `sp-disfluencies` |
| Semantics and Pragmatics | Homer, *Neg-raising and positive polarity: The view from modals* | 2015 | 10.3765/sp.8.4 | CC BY 3.0 | article page | 177 | `sp-neg-raising-modals` |

### Extra formats

| Journal | Article | Year | DOI | Licence | Checked on | Cited by | Source id |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| FQS | Unger, *Partizipative Gesundheitsforschung: Wer partizipiert woran?* (de) | 2012 | 10.17169/fqs-13.1.1781 | CC BY 4.0 | article page | 59 | `fqs-participatory-health-research` |
| Z. Erziehungswiss. | Helm et al., *Was wissen wir über schulische Lehr-Lern-Prozesse im Distanzunterricht während der Corona-Pandemie? – Evidenz aus Deutschland, Österreich und der Schweiz* (de) | 2021 | 10.1007/s11618-021-01000-z | CC BY 4.0 | article page | 147 | `zfe-distance-learning-covid` ¹ |
| Cad. Saúde Pública | Ozamiz-Etxebarria et al., *Niveles de estrés, ansiedad y depresión en la primera fase del brote del COVID-19 en una muestra recogida en el norte de España* (es) | 2020 | 10.1590/0102-311X00054020 | CC BY 4.0 | article page | 910 | `csp-covid-stress-spain` |
| Ecología Austral | Oyarzabal et al., *Unidades de vegetación de la Argentina* (es) | 2018 | 10.25260/EA.18.28.1.0.399 | CC BY 3.0 (article licence line; the journal policy text cites CC BY 4.0) | article page | 650 | `ecoaustral-vegetation-argentina` |
| NACA (NTRS) | Prandtl (translation), *Theory of Lifting Surfaces* | 1920 | — | Public domain in the United States (US federal publication, 1920); NTRS: public use permitted | record + PDF | — | `naca-lifting-surfaces-scan` |
| NACA (NTRS) | Wilson, *Dynamic stability as affected by the longitudinal moment of inertia* | 1924 | — | Public domain in the United States (US federal publication, 1924); NTRS: public use permitted | record + PDF | — | `naca-report-172-scan` |
| Zenodo | Murphy, *Writing Clean Scientific Software* | 2023 | 10.5281/zenodo.8185113 | CC BY 4.0 | record + PDF | 0 | `slides-clean-scientific-software` |
| Language Science Press | Míša Hejná, *A history of English* | 2022 | 10.5281/zenodo.6560337 | CC BY 4.0 | record + PDF | 1 | `langsci-history-of-english` |
| C. R. Biologies | Ropars et al., *La domestication des champignons Penicillium du fromage* (fr) | 2020 | 10.5802/crbiol.15 | CC BY 4.0 | article page | 9 | `crbiol-penicillium-domestication` |
| C. R. Mécanique | Marigo, *La mécanique de l’endommagement au secours de la mécanique de la rupture : l’évolution de cette idée en un demi-siècle* (fr) | 2023 | 10.5802/crmeca.156 | CC BY 4.0 | article page | 7 | `crmeca-damage-mechanics` |
| Zenodo | Bosman et al., *OA Diamond Journals Study. Part 1: Findings* | 2021 | 10.5281/zenodo.4558704 | CC BY 4.0 | record + PDF | 146 | `report-oa-diamond-journals` |

## Dropped: Materials & Design

Dropped by the maintainer on 2026-10-06, without a substitute. ScienceDirect shows an interactive
bot challenge, so neither the article pages nor the PDFs could be opened, and the repository copies
found carry conflicting rights labels (Figshare: all rights reserved; NORA: CC BY-NC-ND), although
Crossref lists CC BY 4.0.

## Replacements

Each replacement is the next most cited CC BY article from the same journal, skipping ones that
repeat a topic already in the corpus.

| Journal | Dropped | Reason | Replaced by |
| --- | --- | --- | --- |
| Advanced Photonics | Galiffi et al. 2022, *Photonics of time-varying media* | SPIE serves neither page nor PDF to scripts or the browser | Huang et al. 2021, *Pushing the limit of high-Q mode…* |
| JACS Au | Rorrer et al. 2021, polyolefin waste | CC BY-NC-ND | Grimm et al. 2021, deuterated auxochromes |
| JACS Au | *Frontier Molecular Orbitalets* 2022 | CC BY-NC-ND | Shire and Anderson 2023, bicyclo[1.1.1]pentanes |
| Chemical Science | *Electro-organic synthesis* 2020 | CC BY-NC | Hoke et al. 2015, mixed-halide perovskites |
| Electron. J. Stat. | Rothman et al. 2008 | Society copyright, no CC licence | Piironen and Vehtari 2017, horseshoe priors |
| Electron. J. Stat. | Ravikumar et al. 2011 | Society copyright, no CC licence | Kennedy 2023, doubly robust estimation |
| Bayesian Analysis | Gelman 2006 | Society copyright, no CC licence | Letham et al. 2019, constrained Bayesian optimisation (the next one, Yao et al. on stacking, is a second discussion paper by the R̂ group) |
| Psychometrika | Sijtsma 2009 (named as a 2025 version) | CC BY-NC 2.0 | McNeish and Matta 2025, SRMR with covariates |
| Psychometrika | Epskamp, *Psychometric network models* | Kept: CC BY 4.0; year corrected to 2020 | — |
| Large-scale Assess. Educ. | `to select` | — | Lorah 2018, multilevel effect sizes |
| J. Labour Market Res. | *The "task approach" to labor markets* 2013 | No CC licence in Crossref or OpenAlex (Springer TDM licence only; pre-2016, before the journal went open access) | Handel 2016, O*NET content model |
| J. Labour Market Res. | *IAB Establishment Panel* 2013 | No CC licence in Crossref or OpenAlex (Springer TDM licence only; pre-2016, before the journal went open access) | Iwasaki and Ma 2020, gender wage gap in China |
| Economics | `to select` | Early volumes are CC BY-NC 2.0 DE; *Shadow economies* 2007 rejected for that | Barbier and Burgess 2017; Chetty et al. 2018 |
| Sci. Technol. Adv. Mater. | Kamiya et al. 2010, IGZO transistors | © NIMS, no CC licence | Yang et al. 2018, graphene disorders (the more cited additive-manufacturing review was skipped: topic) |
| Collabra: Psychology | Diener et al. 2018, subjective well-being | UC Press page and PDF only behind an interactive challenge; PMC holds an author manuscript | Saunders et al. 2018, self-control and inhibition |
| JAIR | Kirk et al. 2023, zero-shot generalisation in RL | Vol. 76, before the CC BY switch | Mandi et al. 2024, decision-focused learning |
| Semantics and Pragmatics | Roberts 2012; McCready 2010 | PDFs say CC BY-NC 3.0 (vol. 1–6) | Ginzburg et al. 2014; Homer 2015 |

## Rejected extras

| Candidate | Reason |
| --- | --- |
| Zeitschrift für Soziologie 2021 | No CC licence shown on the article page |
| Cybergeo 2011, Semen 2018 (OpenEdition) | CC BY 4.0 for text, but OpenEdition serves the PDF to subscribers only |
| Économie et Statistique (Persée) | Persée terms, not CC BY |
| IPBES Global Assessment summary for policymakers | Zenodo record says CC BY 4.0, but the PDF forbids commercial use without permission |
| NACA Report 824 and the 1945 airfoil data summary | Scans with OCR, 261 and 487 pages; shorter NACA items chosen instead |

Pages, layout styles and quality labels are next: the maintainer reviews pages and fills
`expected_quality`, `quality_notes` and more specific `styles` (currently only text-layer status).
