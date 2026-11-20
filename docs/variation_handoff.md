**Subject: Variation pipeline: please check it works, then build the first review set**

Hi Enrique and Samantha,

We now have a pipeline that takes samples we already extracted CI parameters for, swaps **one** CI parameter for a random value from the **same domain**, and has an LLM rewrite the original text / vignette / story so it expresses the new parameter. Nobody has run it with a real model yet (it was only tested with a scripted mock), so there are two jobs: (1) make sure it really produces good text, (2) build the first set for us to review. Please do 1 before 2.

> **Engine/model for this round:** `ENGINE=____  MODEL=____`  (Leon fills this in. Please both use the same one, so the two halves are comparable. `ENGINE` is `vllm`, `openai` or `anthropic`.)

---

## What the code does (2-minute overview)

Everything lives in `Privacy_Audit/`: code in `src/ci_audit/variation/`, command `python -m ci_audit vary`, details in the README section "Variations".

1. **Samples.** It reads the samples with extracted CI parameters: ConfAIde, PrivacyLens and MPCI-Bench from `data/processed/expanded_pilot/`, AirGapAgent-R and PiSAs from `batch1/`. Each sample has its text, its CI parameters (sender, subject, recipient, information type, transmission principle) and its reviewed domain.
2. **Pools.** For every domain there is a pool of roles, attributes and transmission principles, built from `expanded_pilot/knowledge/domain_knowledge.md` and `knowledge/norm_elements_enrique.csv`. Roles can fill sender, subject or recipient; attributes fill information type; transmission principles replace transmission principles.
3. **Replacement (no LLM).** For each CI parameter that has a value in the sample, one variant is created: that parameter is replaced by a random value from the sample's domain pool (seeded, so it is reproducible). It never picks the original value, another role already in the flow, or a value extracted from the same record. If a domain has no usable value (transmission principles are thin), it falls back to other domains and flags the row `cross_domain_fallback`.
4. **Rewrite (LLM).** The model gets the original text, the original and new CI parameters and a format hint for the dataset (one sentence for ConfAIde 2a, narrative for 2b, story, vignette, agent trajectory, ...). It must change only what the new value requires and report which parameters its new text expresses. `--verify` adds a second call that checks the new text against the original. PrivacyLens/MPCI-Bench `seed` texts (the `- sender: ...` lists) are edited directly without an LLM.
5. **Output** in `data/processed/variations/<your output dir>/<engine>__<model>/`: `variations.jsonl` and `.csv` (one row per variant: old/new text, old/new value, changed parameter, domain, provenance of the replacement, verifier result), `failures.jsonl`, `plan.json`, `run_manifest.json`.

Useful flags: `--datasets a,b` · `--per-dataset N` · `--per-domain N --domains-per-dataset M` · `--domains Healthcare,Education` · `--params sender,recipient` · `--input FILE` (your own subset) · `--plan-only` (no LLM, just shows what would be generated) · `--verify` · `--resume` / `--force` · `--concurrency N`.

## Setup

```bash
cd Privacy_Audit
# use an env that has the SDK for your engine (openai for vllm/openai, anthropic for anthropic); then:
PYTHONPATH=src python -m pytest tests/test_variation.py -q      # should print: 18 passed
```
- **vllm:** the server must already be running (`scripts/serve_vllm.sh <model>`). Agent trajectories and PiSAs documents are long, so serve with `MAX_MODEL_LEN=16384` or more. If `PYTHONPATH=src` is not needed in your setup (package installed), drop it.
- **openai / anthropic:** `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in the environment or `.env`.
- Replace `python -m ci_audit` with `PYTHONPATH=src python -m ci_audit` if the package is not installed.

## Step 1: make sure it generates good text (each of you, on your own datasets)

Samantha: `confaide_tier2,confaide_tier3,privacylens`. Enrique: `mpci_bench,airgapagent_r,pisas`.

```bash
python -m ci_audit vary --engine $ENGINE --model $MODEL \
    --datasets <YOUR 3 DATASETS> --per-dataset 1 --verify \
    --output-dir data/processed/variations/smoke/<yourname>
python scripts/variation_review_sheet.py data/processed/variations/smoke/<yourname>/${ENGINE}__*
```
Then read `review_sheet.md` (original and new text side by side). It is **ready** when:
- `failed: 0` (or every failure is understood. Look at `failures.jsonl`). Nothing is empty, cut off or identical to the original.
- In the rows you read (at least one per dataset, including a trajectory and a PiSAs document), the **new value is expressed**, the **old value is gone**, the **other four parameters are unchanged**, and the **format is kept** (same sentence template, same Action/Observation layout, same cast/notes layout).
- The verifier passes on most rows. A few fails are fine if you agree with it; a lot of fails means the text is not right.

If something is wrong: **do not edit the prompts or pools yourselves.** Send Leon the `failures.jsonl` lines (or two bad examples with `variant_id`) and what you expected. Slow or truncated output on long texts: lower `--concurrency`, raise `--max-output-tokens`, or serve with a longer context.
Do not reuse the smoke directory for the real run (use the paths below).

## Step 2: build the initial set (3 domains × 3 samples per dataset)

The sampling is seeded and independent per dataset, so each of you gets exactly your half, and the result is the same as if one person ran everything. **Do not change `--seed`.** Each of you has 3 datasets → 9 domain-groups → **27 samples** (≈118 variants for Samantha, ≈95 for Enrique; each sample yields one variant per CI parameter it has).

Samantha:
```bash
python -m ci_audit vary --engine $ENGINE --model $MODEL --verify \
    --datasets confaide_tier2,confaide_tier3,privacylens --per-domain 3 --domains-per-dataset 3 \
    --output-dir data/processed/variations/initial_review/samantha
```
Enrique:
```bash
python -m ci_audit vary --engine $ENGINE --model $MODEL --verify \
    --datasets mpci_bench,airgapagent_r,pisas --per-domain 3 --domains-per-dataset 3 \
    --output-dir data/processed/variations/initial_review/enrique
```
Add `--plan-only` first if you want to see the 27 samples and their domains before spending any tokens. If a run is interrupted, repeat the same command with `--resume`; failed variants are retried then.

## Step 3: prepare the review sheet and send it back

```bash
python scripts/variation_review_sheet.py data/processed/variations/initial_review/<yourname>/${ENGINE}__* \
    --out data/processed/variations/initial_review/review_<yourname>
```
This writes `review_<yourname>.csv` (fill the reviewer columns with y / n / ?: new value expressed, old value gone, other parameters preserved, natural + same format, replacement value sensible) and `review_<yourname>.md` (side-by-side reading view). Please review **your own rows** and, if time allows, swap halves for a second opinion on a few rows each.
Please mark `replacement_value_sensible = n` whenever the pool value itself is odd for that role (some pool entries are long or mention other scenarios' names, e.g. an information type like "Sarah's clinical status, including admission, ..."). That tells us how good the pools are.

**Send back:** the two review CSVs, plus a short note with: failed variants (if any), the failure types you saw, and anything the rewrite systematically gets wrong (e.g. adds consent facts, changes names, drops the final question in ConfAIde tier 3).

Thanks!
Leon
