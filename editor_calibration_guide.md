# Editor Calibration Guide — CFTR PheWAS Podcast

**Generated:** 2026-05-28  
**Listener:** Runyu (evan891022@gmail.com)  
**Calibration sources:** (1) CFTR PheWAS knowledge quiz, 20 questions, score 15/20; (2) full audit of 368 historical episode scripts in this pipeline  

---

## Audit note: historical script coverage

**Result: zero CFTR episode coverage in the archive.**

All 368 script files in `podcasts/` and `cowork_outbox/` cover Maryland local politics (Montgomery County council races, state legislature, school board, county executive primary). No prior episode has introduced, defined, or explained any CFTR, genomics, or research-methodology concept. Every calibration decision below is therefore derived from quiz performance alone — not from "already covered in Episode X."

**On difficulty tags:** The pipeline's `level` field tags events as `federal / state / county / school / local`. There is no concept-difficulty tag on episodes. The functional equivalent for CFTR episodes is proposed at the end of this document.

---

## Section 1 — ASSUME KNOWN: never explain these again

The listener demonstrated command of these on the quiz. Referencing them without a definition is correct. A brief in-line gloss (one clause, not a full explanation) is fine the *first time* a term appears in any single episode, but do not define it as if the listener has not seen it before.

| Concept | How to reference it in script | Evidence of knowledge |
|---|---|---|
| **Cystic fibrosis** | Say "cystic fibrosis" or "CF" without a disease description. | Quiz baseline — never missed a question requiring basic CF knowledge. |
| **GWAS vs. PheWAS distinction** | Use both terms as shorthand. JORDAN should not ask "what is a GWAS?" — it is known. If PheWAS is introduced in an episode, one clause ("a PheWAS tests one variant against many outcomes — the mirror of a GWAS") is enough; no full re-derivation. | Strong quiz performance on study-design questions. |
| **KNoRMA** | Reference as "the survival-adjusted lung function score" on first mention per episode; no derivation of the Schluchter/survival-adjustment methodology. | Quiz showed confident recall; listener knows it is a phenotype not a raw FEV1. |
| **KING → BOLT-LMM pipeline** | Can say "after kinship-based relatedness filtering and mixed-model association" without explaining what KING or BOLT-LMM are. | Correct on pipeline question. |
| **TWAS** | "Transcriptome-wide association" — use the abbreviation freely; one-line gloss on first appearance per episode is fine but not a tutorial. | Correct on TWAS concept question. |
| **Ivacaftor / CFTR modulator mechanism** | Can say "a CFTR potentiator" without explaining channel-gating biology. The listener understands the therapeutic class. | Correct on modulator question. |
| **CFFPR (CFF Patient Registry)** | Use as a proper noun. One clause ("the CFF Patient Registry, ~30,000 US patients with CF") is sufficient context; no need to explain registry methodology. | Correct on registry question. |
| **SLC26A9 and pleiotropy concept** | Can say "SLC26A9 shows pleiotropy across CFRD, meconium ileus, and ivacaftor response" without defining pleiotropy. | Correct on SLC26A9 pleiotropy question. |
| **Bonferroni correction (concept)** | JORDAN should not ask "what is multiple testing?" The concept is known. See Section 2 for the nuance around threshold arithmetic and FDR — that part is not known. | Correct on Bonferroni concept; missed on threshold calculation and FDR. |
| **TOPMed / WGS cohort structure** | Can refer to "the CFF Genome Project WGS cohort (~5,000 participants)" without explaining what whole-genome sequencing is. | No quiz misses on WGS study-design questions. |
| **Pancreatic insufficiency as CF stratifier** | Can use as a covariate label without explanation. | Correct on questions requiring this as background knowledge. |

---

## Section 2 — CALIBRATED EXPLANATION: explain once per episode, then assume

These concepts are partially known or were confidently missed on the quiz. In any episode where they arise, give one clear explanation (JORDAN asks, ALEX answers — 2–4 lines). After that episode, treat as known and do not re-explain. If a later episode references the concept only in passing, a one-clause gloss is fine; a full re-definition is not.

### 2a. EHF/APIP biology at chr11p13

**What the listener missed (Q8):** Understood the locus is a CF modifier; did not know that EHF is a *transcriptional regulator* of CFTR (not a structural CFTR protein) and that APIP is functionally unrelated (methionine salvage / anti-apoptotic).

**How to explain it once:**  
> JORDAN: So EHF is at the same locus as APIP — are they doing the same thing?  
> ALEX: No, completely different jobs. EHF is a transcription factor — it controls how much CFTR gets produced in the first place. Think of it as a volume dial on the CFTR gene. APIP is right next door on the chromosome but does something unrelated: it mops up toxic metabolites and helps cells survive inflammatory stress. Same genetic address, different machinery.

After this explanation appears once, later episodes can say "the EHF transcriptional regulator at 11p13" without re-explaining what a transcription factor does.

### 2b. FDR (False Discovery Rate) vs. Bonferroni — the distinction

**What the listener missed (Q9):** Knows Bonferroni exists and the general idea of multiple testing. Did not know FDR = Benjamini-Hochberg q-value; confused the threshold calculation (Bonferroni ≈ 8×10⁻⁶ for this PheWAS); unclear on what FDR controls vs. what Bonferroni controls.

**How to explain it once:**  
> JORDAN: I keep seeing q-values in these papers alongside p-values. They're not the same thing?  
> ALEX: Different questions. Bonferroni asks: what threshold guarantees I get zero false positives across all my tests? FDR — the Benjamini-Hochberg q-value — asks: what threshold means only 5% of my significant results are false? Bonferroni is stricter. For this PheWAS, 150 variants times 40 outcomes gives a Bonferroni cutoff around 8 times ten to the minus six. FDR lets more signals through but you're accepting that some are noise.

After this, "FDR-corrected" and "Bonferroni threshold" can be used as shorthand; the distinction does not need to be re-litigated.

### 2c. Dysanapsis

**What the listener missed (Q12):** Had not heard the term. Answered incorrectly on the airway-to-lung-volume mismatch concept; did not connect it to CF modifier loci acting via developmental pathways.

**How to explain it once:**  
> JORDAN: They keep mentioning dysanapsis in the lung development papers — what is that?  
> ALEX: During fetal development, airways and air sacs are supposed to grow at the same rate. Dysanapsis is when they don't — you end up with airways that are narrow relative to the lung volume they're supposed to ventilate. It's set at birth, doesn't reverse, and predisposes you to obstructive physiology even before any infection happens. In CF, if a modifier gene acts through dysanapsis, it's adding a second strike on top of the CFTR problem — smaller plumbing and a broken pump.

After this episode, "dysanapsis" can appear without definition. "Airway caliber mismatch" is always an acceptable one-clause gloss.

### 2d. TCF7L2 and the shared beta-cell mechanism with T2D

**What the listener missed (Q17):** Answered D (SLC26A9 is associated with general T2D) rather than B (TCF7L2 shows shared T2D signal; SLC26A9 does not). The error is a swapped assignment — the listener knows SLC26A9 is a CFRD modifier but incorrectly attributed the T2D overlap to it rather than to TCF7L2.

**How to explain it once:**  
> JORDAN: So both SLC26A9 and TCF7L2 show up in CFRD genetics. Are they doing the same thing?  
> ALEX: No — completely different stories. TCF7L2 is the strongest Type 2 diabetes locus in the general population. It also turns up in CFRD, which means there's a beta-cell dysfunction component to CFRD that's shared with ordinary T2D — same wiring problem, two different buildings. SLC26A9 is the opposite: it's a strong CFRD modifier and shows meconium ileus pleiotropy, but there's no T2D signal in the general population. SLC26A9 is CF-specific; TCF7L2 is cross-disease.

After this, JORDAN should not confuse the two again. Later scripts can say "the shared T2D/CFRD TCF7L2 signal" or "the CF-specific SLC26A9 axis" without re-establishing the distinction.

---

## Section 3 — STILL EXPLAIN: always scaffold these

These are concepts where the listener showed a gap AND no prior episode has introduced them. No "explained once and still missed" pattern exists (there are no prior CFTR episodes), so this section flags concepts requiring careful, concrete scaffolding on first exposure — and if any first-exposure episode gets written and the listener still struggles, the script should be revised before the next appearance.

### 3a. CHP2/SLC9A3 regulatory axis

**Why it's here:** Q13 miss — too specific for the listener. The mechanistic chain (CHP2 activates NHE3 = SLC9A3, a sodium-hydrogen exchanger on airway epithelium, explaining why two independent GWAS hits converge) was not retained.

**Scaffolding rule:** Do not explain the full CHP2→NHE3 chain in passing. Either give the full version (Faino 2025 showed that CHP2, a regulator of the SLC9A3 sodium-hydrogen exchanger, independently modifies chronic Pa infection risk — two separate GWAS signals, same ion channel axis) or reduce it to a one-clause label: "two independent hits converging on the same airway ion channel, SLC9A3." The middle ground — assuming partial knowledge — produces confusion.

**Flag for re-do:** If any future episode introduces this at medium depth and post-episode quiz or note feedback shows the listener still can't reconstruct which gene does what, rewrite that segment at full scaffolding depth.

### 3b. Bonferroni threshold arithmetic

**Why it's here:** Separate from the concept (Section 2b), the listener specifically missed the threshold *calculation* — deriving 8×10⁻⁶ from 150 variants × ~40 outcomes. This is a detail that can be scaffolded in a methods episode:

> ALEX: It's just division. Bonferroni says: take your standard threshold — 0.05 — and divide by the number of tests. Six thousand tests gives you roughly 8 times ten to the minus six. If your p-value clears that bar, you've controlled the probability of even one false positive across all six thousand comparisons.

Once this arithmetic has been walked through, it should not need repeating. If it is still missed in a post-episode check, use a worked numerical example.

---

## Difficulty tag mapping

**Current pipeline behavior:** The `level` field on events tags content by political tier: `federal`, `state`, `county`, `school`, `local`. There is no concept-difficulty tag in the existing schema.

**Proposed equivalent for CFTR episodes:** Tag each episode or segment with a content tier using this mapping:

| Pipeline tag | CFTR equivalent | Meaning |
|---|---|---|
| `federal` | `methods` | Statistical/computational methods (GWAS, TWAS, multiple testing, imputation). Highest abstraction; listener needs scaffolding. |
| `state` | `genetics` | Genetic loci, modifier biology, variant interpretation. Intermediate; listener is partially fluent. |
| `county` | `clinical` | Clinical outcomes, phenotypes, registries, pharmacogenomics. Closest to what the listener can anchor to without derivation. |
| `school` | `regulatory` | Drug approval, modulator access, FDA decisions. Listener does not need genomics background to follow. |

**Applied to quiz-weak topics:**

| Topic | Proposed tag | Implication |
|---|---|---|
| EHF/APIP transcriptional biology | `genetics` | One explanation needed; can reference freely after |
| FDR vs Bonferroni distinction | `methods` | Full scaffolding required; arithmetic walkthrough helpful |
| Dysanapsis | `genetics` → `clinical` | Explain in a `genetics`-tagged segment; subsequent `clinical` references can use it as known |
| CHP2/SLC9A3 axis | `genetics` | Full chain or one-clause only; no medium-depth shorthand |
| TCF7L2/CFRD T2D overlap | `genetics` → `clinical` | Explain once in a `genetics` segment; then freely use in `clinical` CFRD episodes |

---

*This file is the source of truth for listener calibration. Update it after any post-episode knowledge check or quiz. The three sections above should be revised whenever new CFTR episodes are generated and concept coverage accumulates.*
