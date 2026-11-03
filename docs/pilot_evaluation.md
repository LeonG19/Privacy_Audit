# Model-comparison pilot

Goal: decide which model to use for the full extraction + domain-classification pipeline by running every
candidate on the **same** small sample and scoring it against a **human reference**. Proprietary models
(Claude, GPT) and the open-source model (Llama-3.3-70B via vLLM) go through the same `extract` command, the
same prompts and the same scorer, so the only difference between runs is the model.

Nothing here processes the full datasets. Scale-up waits for the review meeting.

## 1. The shared sample (done once, already committed)

```bash
python -m ci_audit pilot sample          # reads configs/pilot.yaml
```

- 20 records per dataset (100 total), seed `20261102`, stratified evenly over the subsets the full pipeline
  would process: ConfAIde Tier 2 (2a, 2b), PrivacyLens (seed, vignette, trajectory), MPCI-Bench (seed, story,
  trajectory), AirGapAgent-R (full), PiSAs (original, expanded). At most one record per underlying scenario
  (one level of a PrivacyLens case, one side of an MPCI pair, one of the 2a/2b rewrites).
- `pilot/pilot_sample.json` holds the record ids, their content hashes and the population sizes per subset (used
  for cost extrapolation). Every run passes `--ids-file pilot/pilot_sample.json`.
- The command refuses to overwrite the sample or the reference sheet.

## 2. Runs

All pilot runs use **`--blind-native-ci`**. Without it, PrivacyLens and MPCI-Bench give the model all five CI values
(and AirGapAgent-R and PiSAs give some of them). The model is told not to extract those values and its output for them is discarded, so
extraction accuracy could not be measured on those datasets. In blind mode the model extracts all five from the
text. The benchmark values stay in the record's `native_ci` and appear as hints (`bench_*`) in the reference
sheet. The mode is recorded in `prompt_version` (`…+blind_native_ci`) and in the run manifest.

Outputs go to `pilot/runs/<dataset>/<engine>__<model>/`.

### Llama-3.3-70B (vLLM)

Model: `RedHatAI/Llama-3.3-70B-Instruct-quantized.w4a16`, INT4 weights with 16-bit activations, about 40 GB. It
needs a GPU with compute capability 8.0 or higher (A100, L40S, H100 and similar; V100s cannot run it):
one 80 GB GPU, or two 40–48 GB GPUs with `TP=2`. Accept the Llama licence on Hugging Face and export `HF_TOKEN`.

```bash
pip install -e . vllm
# Slurm (edit partition / GPU type / account first):
sbatch scripts/slurm/pilot_llama70b.sbatch
# or directly on a GPU machine:
scripts/run_pilot_vllm.sh                       # CONCURRENCY=8 TP=1 MAX_MODEL_LEN=16384 by default
```

After a Slurm timeout or crash, resubmit with `RESUME=1`: datasets already started continue where they stopped.
GPU figures cover the inference window only (not the model download/load), so the cluster bill is somewhat higher.

`run_pilot_vllm.sh` starts the server (`scripts/serve_llama70b.sh`, with prefix caching on and the checkpoint's
sampling defaults off, so temperature 0 and the seed apply), waits until it is ready, and logs GPU utilisation, memory
and power once per second to `pilot/runs/gpu_metrics.csv` (`python -m ci_audit gpu-monitor`). It then extracts
the five datasets at a fixed concurrency and shuts everything down. vLLM's version and the GPU inventory are saved
next to the runs.

### API models

Same command, `--engine anthropic|openai --model <exact model id>`, with `--ids-file pilot/pilot_sample.json
--blind-native-ci --output-dir pilot/runs` and the default generation settings (temperature 0, 3000 output tokens).
Add the price per 1M tokens, and the date it was looked up, under `costs.api_pricing` in `configs/pilot.yaml`.

## 3. Human reference (`pilot/reference_annotations.csv`)

Annotate from the text in `pilot/pilot_scenarios.md` (exactly what the models see). Do this **before looking at
model outputs**, and do not copy model outputs into the reference. Two annotators reconcile disagreements in
`notes`.

| column                                                                                                  | what to write                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `gold_sender`, `gold_subject`, `gold_recipient`, `gold_information_type`, `gold_transmission_principle` | The value as the text states it. Several acceptable wordings are separated by `\|` (e.g. `your school \| your school or university`). Write `<missing>` when the text does not state it. A purpose is not a transmission principle (see `prompts/shared/extraction_rules.md`, which the models also follow). Leave the cell empty only if not yet annotated. |
| `gold_domain`                                                                                           | One label from `configs/domains.yaml` (or `Unclear` / `Other`).                                                                                                                                                                                                                                                                                              |
| `gold_domain_alt`                                                                                       | Other labels you would also accept, `\|`-separated (domains can be legitimately ambiguous).                                                                                                                                                                                                                                                                  |
| `gold_privacy`                                                                                          | Optional, and only where the benchmark gives a valid reference for appropriateness; otherwise leave empty. Scored separately from domain.                                                                                                                                                                                                                    |
| `bench_*`                                                                                               | Prefilled from the benchmark's own fields. These are hints only: check them against the text before using them.                                                                                                                                                                                                                                              |

## 4. Scoring

```bash
python -m ci_audit pilot score --gpu-log pilot/runs/gpu_metrics.csv      # all runs under pilot/runs
```

Writes `pilot/reports/report.md` and `scores.json`. When a model value is neither equal to a reference value
nor close to it (token-set F1 ≥ 0.8), the scorer adds the pair to `pilot/judgments.csv` with an empty
`verdict`. Fill in `correct`, `partial` or `incorrect` and re-run the scorer. Verdicts are never overwritten, and
the same value from another model reuses the verdict. Until the report says 0 pairs are pending, its accuracy is
provisional.

What the report contains, per model and per dataset:

- **CI accuracy per parameter**, strict, with a 95% Wilson interval. With 20 records per dataset, differences inside
  the interval are not evidence.
- **Missing information.** _Invention rate_ is how often the model gives a value where the reference says `<missing>`.
  _Miss rate_ is how often it returns null where the text states a value. The report also counts records with
  evidence quotes that are not in the source, and claims of consent, restrictions or relationships that the
  validator dropped as unsupported.
- **Domain accuracy**, strict and with accepted alternatives.
- **Output validity**: valid JSON or schema on the first try, re-asks, failures by type.
- **Time, tokens, cost.** Tokens are counted over every call, re-asks included. The report also gives latency, wall
  time, records per minute and output tokens per second. API cost comes from `api_pricing`. For vLLM it gives
  GPU-hours, mean GPU utilisation, peak memory and energy, plus a dollar figure if `gpu_hourly_usd` is set.
- **Full-dataset estimate**: per-subset pilot means × subset population, at the pilot's concurrency. It is
  conservative for vLLM, which batches better at higher concurrency.
