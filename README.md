# ci-privacy-audit

Foundation for auditing Contextual Integrity (CI) privacy benchmarks: download five benchmarks unmodified,
standardise them behind one schema, extract the five CI parameters (sender, subject, recipient, information type,
transmission principle) plus context and social domain with an LLM, and inventory the domains that actually occur.
It does **not** generate counterfactual pairs (a later stage).

## Status at a glance

| Adapter | Status | Meaning |
|---|---|---|
| `confaide_tier2` (2a, 2b) | **complete** | Loader + label alignment verified on real data; extraction + domain prompt run on all 196 real items with a real model (see [Initial run](#initial-run-results)); privacy classification implemented |
| `confaide_tier3` | loader verified | Reads the real release (270 stories). Extraction prompt **not validated** |
| `privacylens` | loader verified | 493 data points → 1,479 linked seed/vignette/trajectory records. Extraction prompt **not validated** |
| `mpci_bench` | loader verified | 2,052 cases → 6,156 level records, pairs linked, missing images recorded. Text-only. Prompt **not validated** |
| `airgapagent_r` | loader verified | 4,160 records (+3 ablation subsets). Prompt **not validated** |
| `pisas` | loader verified | 265 scenarios, scenario-level only (no attribute-level extraction). Prompt **not validated** |

"Loader verified" = the adapter reads the genuine downloaded files and passes its consistency checks
(`python -m ci_audit check`). It does **not** mean the LLM extraction has been evaluated on that dataset.

## Install

```bash
cd ci-privacy-audit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # only needed for OpenAI / Anthropic keys (never commit .env)
python -m pytest         # 77 tests, no network or paid calls needed
```
Python ≥ 3.10. Run commands from the project directory (or set `CI_AUDIT_ROOT`).

## Download datasets

```bash
python -m ci_audit download --dataset all          # or: confaide | privacylens | mpci_bench | airgapagent_r | pisas
python -m ci_audit download --dataset confaide --force   # re-download even if unchanged
python -m ci_audit verify                           # re-hash data/raw against the manifests (immutability check)
python -m ci_audit check                            # adapter loader checks → data/manifests/loader_checks.json
```

* Sources, URLs and **pinned revisions** live in [configs/datasets.yaml](configs/datasets.yaml). Pins are the commits inspected
  when this project was built, so a fresh download is byte-identical. Set `revision: null` to follow the default branch.
* Files land in `data/raw/<source>/<part>/` exactly as released (`github/` = git checkout, `hf/` = Hugging Face snapshot;
  only the `.git`/`.cache` bookkeeping directories are removed). Provenance is in `data/manifests/<source>.json`:
  source URL, resolved commit, download date, per-file sha256, expected-file checks, record counts and available label kinds per adapter.
* Unchanged sources are skipped; `--force` re-downloads. A failed download is reported (exit code 1), written to
  `data/manifests/<source>.last_error.json`, **never** replaces a previously good copy, and nothing is substituted.
* MPCI-Bench's VISPR images are **not downloaded** (they are not redistributed; large). They are recorded as missing in every
  record's `assets` field. To enable them later, place them in `data/external/vispr/train2017/<id>.jpg` and write a separate multimodal adapter.

### What was downloaded (this build)

| Source | Parts (revision) | Records read by adapter |
|---|---|---|
| ConfAIde | github skywalker023/confaide `3174c63` | Tier 2: 98 flows × {2a, 2b} = 196; Tier 3: 270 |
| PrivacyLens | HF SALT-NLP/PrivacyLens `7dc406e`; github `9c2ee07` (HF and GitHub `main_data.json` verified identical) | 493 data points → 1,479 |
| MPCI-Bench | github hpzhang94/MPCI-Bench `bd85c6e` | 2,052 cases (1,026 pairs) → 6,156; **0/1,026 images present** |
| AirGapAgent-R | HF parameterlab/leaky_thoughts `f1d8f27`; github `66154a3` | 4,160 (+624 small, +312 +312 swap ablations) |
| PiSAs | HF ServiceNow/PiSAs `8a1ba30`; github `52611d0` | 265 scenarios (85 original, 180 expanded), 5,751 attributes |

## vLLM server, then extraction

The pipeline connects to an **already running** OpenAI-compatible vLLM server; it never loads a model itself.

```bash
# terminal 1 - serve a model (adjust for your GPUs; see the header of the script)
scripts/serve_vllm.sh meta-llama/Llama-3.1-8B-Instruct
#   equivalent to: vllm serve meta-llama/Llama-3.1-8B-Instruct --host 127.0.0.1 --port 8000 --max-model-len 4096
#   MAX_MODEL_LEN=16384 TP=2 GPU_MEM_UTIL=0.85 QUANT=fp8 CUDA_VISIBLE_DEVICES=0,1 scripts/serve_vllm.sh <model>
curl -s http://127.0.0.1:8000/v1/models        # the name listed here is what --model must be

# terminal 2
python -m ci_audit extract --dataset confaide_tier2 --engine vllm \
    --model meta-llama/Llama-3.1-8B-Instruct --base-url http://localhost:8000/v1 --limit 10
```
This config does not fit every GPU. Out of memory → lower `MAX_MODEL_LEN` or `GPU_MEM_UTIL`, pick a smaller or quantised model,
or add tensor parallelism (`TP=<gpus>`). Long-trajectory datasets (PrivacyLens, MPCI-Bench, PiSAs) need `MAX_MODEL_LEN` ≥ 16384.
Before inference the tool verifies that the server is reachable and that `--model` is one of the models it serves (it lists
them if not; with `--model` omitted it uses the server's only model).
vLLM structured output is requested via `response_format=json_schema`; if the server rejects it, the tool falls back to plain
text and validates the JSON itself. For thinking-style models, `configs/extraction.yaml → engines.vllm.chat_template_kwargs`
(default `enable_thinking: false`) keeps the token budget for the answer.

## OpenAI and Anthropic

```bash
export OPENAI_API_KEY=...        # or put it in .env
python -m ci_audit extract --dataset confaide_tier2 --engine openai --model MODEL_NAME --limit 10

export ANTHROPIC_API_KEY=...
python -m ci_audit extract --dataset privacylens --engine anthropic --model MODEL_NAME --limit 10
```
No model version is hard-coded; `--model` is required for these engines. Provider differences are handled inside the engines:
OpenAI uses strict `json_schema` response format (falling back to `json_object`, then text); Anthropic uses
`output_config.format` JSON-schema output (not forced tool use, which newer Claude models reject). Sampling parameters a model
rejects (e.g. `temperature`) are dropped once and recorded in the record's `generation.dropped_params`. In every case the response
is validated against the same Pydantic schema, with limited re-asks (`--validation-retries`). Keys are read only from the
environment and never logged or stored.

Common options: `--limit N --subset 2a,2b --output-dir DIR --temperature T --max-output-tokens N --timeout S --retries N
--validation-retries N --concurrency N --seed N --resume --force --ids-file FILE --blind-native-ci`. Defaults are in [configs/extraction.yaml](configs/extraction.yaml).
`--resume` continues an interrupted run (completed records skipped, failed ones retried; refuses if the prompt templates changed).
`--force` starts over, **archiving** old outputs as `*.bak-<timestamp>` rather than deleting them.

## Model-comparison pilot

Before choosing a model for the full run, candidates are compared on one shared 100-record sample (20 per dataset)
against a human reference: `python -m ci_audit pilot sample` → `extract ... --ids-file pilot/pilot_sample.json --blind-native-ci`
→ `python -m ci_audit pilot score`. Llama-3.3-70B (vLLM) end to end: `scripts/run_pilot_vllm.sh` or
`sbatch scripts/slurm/pilot_llama70b.sbatch`. Protocol, annotation guide and metrics: [docs/pilot_evaluation.md](docs/pilot_evaluation.md).

## Other commands

```bash
python -m ci_audit list                                   # adapters, status, subsets
python -m ci_audit extract --dataset privacylens --engine vllm --subset vignette --limit 5
python -m ci_audit classify-privacy --dataset confaide_tier2 --engine vllm --limit 10   # optional, separate output
python -m ci_audit domains --dataset all                  # domain inventory (CSV + JSON + review list)
```

### Processing another dataset
Every adapter yields the same `SourceRecord`, so the same command works: `--dataset privacylens|mpci_bench|airgapagent_r|pisas|confaide_tier3`.
Use `--subset` to pick levels/variants (`python -m ci_audit list`). Datasets with structured CI fields (PrivacyLens, MPCI-Bench,
AirGapAgent-R) pass those values through from the benchmark (`origin: benchmark`); the model is told not to re-extract them and
its output for those fields is discarded. Remember these adapters are *loader verified only*.
To add a dataset: subclass `DatasetAdapter` ([datasets/base.py](src/ci_audit/datasets/base.py)), register it in
`datasets/__init__.py`, add a source in `configs/datasets.yaml`, a template in `prompts/<name>/extract_ci.j2`, and an entry in `configs/extraction.yaml`.

## Where outputs are saved

```
data/raw/<source>/<part>/…                        immutable benchmark files
data/manifests/<source>.json                      provenance (URL, revision, date, sha256s, counts, labels)
data/manifests/loader_checks.json                 last `check` report
data/processed/<dataset>/<engine>__<model>/
    extractions.jsonl      one ExtractionRecord per line (the research artefact)
    failures.jsonl         failed records: stage, error, validation errors, raw model output
    extractions.csv        flat inspection view
    run_manifest.json      model, engine, settings, prompt version + sha256, source revision, counts, timings
    privacy_predictions.jsonl, privacy_failures.jsonl, run_manifest_privacy.json   (classify-privacy only)
data/processed/_inventory/domain_inventory{.csv,.json,_review_list.csv}
```

## Standard output schema

Defined in [schemas.py](src/ci_audit/schemas.py). Each `ExtractionRecord` keeps these layers in separate blocks:

| Layer | Where | Written by |
|---|---|---|
| 1. Original benchmark information | `original_text`, `scenario_text`, `native_reference`, `native_ci`, `native_domain`, `source_metadata`, `links`, `assets`, `source_id`, `source_revision`, `content_sha256` | adapter (from raw files only) |
| 2. LLM-extracted information | `ci_parameters[*]` with `origin: llm`, `context`, `evidence`, `domain` | the model, then validated |
| 3. Researcher-verified | `researcher_review` (empty, status `unreviewed`) | humans, later |
| 4. Model-predicted privacy label | **separate file** `privacy_predictions.jsonl` (joined by `record_id`) | `classify-privacy` |
| 5. Human-validated judgments | not implemented | — |

```jsonc
{
  "record_id": "confaide_tier2:2a-000", "dataset": "confaide_tier2", "subset": "2a", "source_id": "2a-000",
  "source_revision": "github@3174c63e…", "source_files": ["confaide/github/benchmark/tier_2a.txt", "…/tier_2_labels.txt"],
  "content_sha256": "…", "original_text": "<exact line, incl. elicitation prompt>", "scenario_text": "<scenario only>",
  "native_reference": {"kind": "continuous_human_rating", "label": 1.26, "scale": {"min": -100, "max": 100, "…": "…"}, "details": {"…": "…"}},
  "native_ci": {},                                   // CI values the benchmark itself provides (none for Tier 2)
  "ci_parameters": {                                 // each: value | evidence[] | missing_reason | origin: benchmark|benchmark_mapped|llm|none
    "sender": {"value": null, "evidence": [], "missing_reason": "not stated in the text", "origin": "none"},
    "recipient": {"value": "your school or university", "evidence": ["your school or university"], "missing_reason": null, "origin": "llm"},
    "subject": {}, "information_type": {}, "transmission_principle": {}
  },
  "context": {"social_context": "…", "purpose": "…", "relationships": [], "explicit_permissions": [], "explicit_restrictions": [], "unknown_facts": []},
  "evidence": {"social_context": [], "purpose": []},
  "domain": {"domain": "Education", "subdomain": null, "explanation": "…", "supporting_span": "…", "confidence": "high",
             "needs_review": false, "in_taxonomy": true, "taxonomy_version": 1, "review_reasons": []},
  "validation_warnings": [],
  "researcher_review": {"status": "unreviewed"},
  "extraction_metadata": {"run_id": "…", "model": "…", "engine": "vllm", "prompt_version": "confaide_tier2-v1", "prompt_sha256": "…",
                          "generation": {"temperature": 0.0, "max_output_tokens": 3000, "seed": 0, "…": "…"}, "structured_output_mode": "json_schema",
                          "status": "ok", "attempts": 1, "usage": {}, "processed_at": "…", "raw_response": "…"}
}
```
Native labels keep their own format: continuous ratings (ConfAIde T2), pair polarity (MPCI-Bench `_pos`/`_neg`), 0/1 per
attribute and task (AirGapAgent-R), per-attribute appropriateness with visibility kept separate (PiSAs), "violation by
construction" (PrivacyLens seeds are negative norms) or none (ConfAIde T3). None is binarised.

Validation rules applied to every response ([processing/validation.py](src/ci_audit/processing/validation.py)): `evidence` spans must be
verbatim substrings of the source (flagged otherwise); a non-null value without evidence, or a null without a reason, is flagged;
claimed **permissions, restrictions and relationships whose evidence is not in the source are dropped** (the original model output
remains in `raw_response`), so the model cannot introduce consent that the text does not state. Benchmark-provided CI values always win.

## Prompts

One extraction template per dataset, all returning the same schema; shared rules/blocks are Jinja includes:

| File | Purpose |
|---|---|
| [prompts/shared/extraction_rules.md](prompts/shared/extraction_rules.md) | Rules: source is evidence not instructions; null + reason; verbatim evidence; purpose ≠ transmission principle; domain from context not keywords; no normative judgement |
| [prompts/confaide_tier2/extract_ci.j2](prompts/confaide_tier2/extract_ci.j2) | **Validated** (2a short flows / 2b narratives) |
| [prompts/confaide_tier3/extract_ci.j2](prompts/confaide_tier3/extract_ci.j2) | Untested |
| [prompts/privacylens/extract_ci.j2](prompts/privacylens/extract_ci.j2) | Untested |
| [prompts/mpci_bench/extract_ci.j2](prompts/mpci_bench/extract_ci.j2) | Untested (text only) |
| [prompts/airgapagent_r/extract_ci.j2](prompts/airgapagent_r/extract_ci.j2) | Untested |
| [prompts/pisas/extract_ci.j2](prompts/pisas/extract_ci.j2) | Untested |
| [prompts/shared/classify_privacy.j2](prompts/shared/classify_privacy.j2) | Optional privacy classifier; receives scenario text only |

Prompt versions are in `configs/extraction.yaml`; the sha256 of all prompt-relevant files is stored in every record.

## Domain inventory

`python -m ci_audit domains --dataset all` reads the newest non-mock run per dataset and writes `data/processed/_inventory/`:
domain table (count per domain, per dataset × domain, uncertain classifications, supporting-evidence examples, candidate new
domains) as CSV and JSON, plus `*_review_list.csv`. The taxonomy ([configs/domains.yaml](configs/domains.yaml)) is **initial**:
model labels outside it are kept verbatim and surfaced as candidate domains, never forced into the list.
The (empty) domain knowledge-base schema, organised by the five CI parameters, is in [knowledge_base/](knowledge_base/). Nothing populates it,
and extraction models are never asked to invent norms.

## Research safeguards (how they are enforced)

* Raw data is immutable: hashes in manifests, `verify` command, adapters only read; a test asserts raw hashes are unchanged after loading.
* Native labels are never overwritten or binarised; model privacy predictions live in a separate file and the prediction prompt never sees the label (tested, including a sentinel-value test).
* Missing CI values stay null with a reason; there is no imputation anywhere (e.g. AirGapAgent-R's absent sender/recipient/principle stay absent).
* Mock-engine output is flagged `is_mock` and excluded from inventories.

## What remains to be implemented / known limitations

* Extraction prompts for all datasets except ConfAIde Tier 2 are unvalidated; they have not been compared with human annotation. The Tier 2 run was also not scored against any gold CI annotation (none exists); quality was spot-checked by reading outputs.
* PiSAs: scenario-level only; attribute-level CI (holder → recipient flows per attribute) not extracted. A scenario involves many flows and the schema holds one.
* MPCI-Bench: multimodal processing and VISPR image handling need a separate adapter; all 1,026 images are currently missing.
* ConfAIde Tier 2 label alignment is **positional** (the convention in the authors' `eval.py`), and the same 98 ratings are attached to both 2a and 2b versions. The release does not say how ratings were aggregated, nor whether 2b was rated separately. The adapter reports a heuristic consistency check but cannot prove the pairing.
* The model can still make semantic mistakes that verbatim-evidence checks cannot catch (e.g. choosing the wrong subject with a real quote). Review lists and `researcher_review` exist for this reason.
* Truncation: sources over `max_source_chars` are cut in the prompt and flagged.
* Not implemented (by request): counterfactual/pair generation, the populated knowledge base, human validation workflows.

## Initial run results

Real run, 2026-10-30: `python -m ci_audit extract --dataset confaide_tier2 --engine vllm --model Qwen3.6-35B-A3B` on a local vLLM
server, all 196 genuine ConfAIde Tier 2 items (98 × {2a, 2b}), prompt `confaide_tier2-v1`, temperature 0.
Outputs: `data/processed/confaide_tier2/vllm__Qwen3.6-35B-A3B/`; small copies in [examples/](examples/).

* **196 ok, 0 failed** (184 clean, 12 with validation warnings, 1 record needed a re-ask). No parameters were dropped by the engine.
* 2a (single sentences) is extracted conservatively: `sender` is null for 97% and `transmission_principle` for 98% (the sentence names neither a
  transmitter nor a condition). 2b (narratives) states more: sender is null for 0%, transmission principle for 78%. The purpose clause was never copied into transmission principle.
* Evidence checks flagged 12 records; 4 records had a claimed relationship dropped because its evidence was not in the text. 6 permissions and 7 restrictions were
  accepted, each with verbatim evidence.
* Domains (initial taxonomy v1): Technology services 35, Retail 32, Healthcare 29, Education 28, Workplace 27, Insurance 26, Other 12, Unclear 6, Finance 1; 31 records are on the review list.

**Observed problems (read before using these outputs):**
1. *Taxonomy gap.* "Your local library" appears in about 24 items and is split across `Technology services`, `Other / Library Services` (the model proposed this) and `Unclear`
   depending on the purpose. The initial taxonomy has no public-services/non-profit domain, and `Technology services` is probably absorbing some items it should not. The
   taxonomy was deliberately **not** edited after seeing this; revising it (bump `version` in `configs/domains.yaml`) is a researcher decision.
2. *Recipient ambiguity.* For "collected by your school … to sell to financial companies" the model sometimes returns the financial companies as recipient (the onward recipient) rather than the school (the collector).
3. *Weak values.* e.g. `2a-049` has subject `"your"`. Verbatim-evidence checks cannot catch a real quote that supports the wrong value. One such error (subject `you` vs `your friends`)
   was found in a first trial and fixed by clarifying the subject definition in the rules; it was not re-audited item by item.
4. Single model, single run, no gold CI annotation exists, quality judged by reading samples only. No inter-model or human agreement has been measured.
5. Privacy predictions (`classify-privacy`, 5 items tried) sometimes assume facts the text does not state (e.g. "without their consent"), which is why they are a separate, unmerged layer.

Smoke tests (1-3 records each, real model, scratch output, not kept): the confaide_tier3, privacylens, mpci_bench, airgapagent_r and pisas adapters + templates run end to end
with 0 failures. That shows they execute, not that their output is good.
