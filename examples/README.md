# Example outputs (REAL, not fixtures)

Produced on 2026-10-30 by `python -m ci_audit extract --dataset confaide_tier2 --engine vllm --model Qwen3.6-35B-A3B --concurrency 6`
against a local vLLM server, on the genuine ConfAIde release (github skywalker023/confaide @ 3174c63, MIT licence).

* `confaide_tier2_extractions_first5.jsonl` - first 5 of 196 ExtractionRecords (full run: 196 ok, 0 failed)
* `confaide_tier2_run_manifest.json` - settings, prompt version + sha256, source revision, counts
* `domain_inventory_confaide_tier2.{csv,json}`, `domain_inventory_review_list.csv` - domain inventory over all 196 records
* `confaide_tier2_privacy_predictions_first5.jsonl` - optional model privacy predictions (separate layer; NOT benchmark labels)

These are single-model, unreviewed LLM outputs (`researcher_review.status = unreviewed`). Do not treat them as ground truth.
