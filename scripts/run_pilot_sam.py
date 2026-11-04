"""Focused CI extraction pilot for OpenAI and Anthropic."""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ci_audit.config import Paths, load_config
from ci_audit.engines.base import GenerationSettings
from ci_audit.engines.openai_api import OpenAIChatEngine
from ci_audit.engines.anthropic_api import AnthropicEngine
from ci_audit.schemas import LLMExtraction, strict_json_schema
from ci_audit.processing.validation import parse_json_response

SAMPLES = ROOT / "data/processed/pilot/pilot_samples.jsonl"
RESULTS = ROOT / "data/processed/pilot/results"

SYSTEM = """
You are a research assistant extracting Contextual Integrity parameters.

Extract all five parameters independently:
sender, subject, recipient, information_type, transmission_principle.

Use only the provided scenario. Do not invent missing information.
Return null for parameters that cannot be determined.

For every non-null parameter, provide an exact evidence span
copied from the scenario.

A transmission principle is a condition governing information flow,
not merely the purpose of the disclosure.

Classify the social domain using the provided taxonomy.
Do not judge whether the information flow is appropriate.

Return JSON matching the supplied schema.
""".strip()


def load_samples():
    with SAMPLES.open(encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    if len(records) != 50:
        raise ValueError(f"Expected 50 samples, found {len(records)}")

    ids = [r["record_id"] for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate record IDs")

    return records


def build_prompt(record, taxonomy):
    domains = "\n".join(
        f"- {name}: {description}"
        for name, description in taxonomy.items()
    )

    return f"""
Classify the scenario using one of these domains:

{domains}

SCENARIO:
<source_material>
{record["scenario_text"]}
</source_material>

OUTPUT SCHEMA:
{json.dumps(strict_json_schema(LLMExtraction), indent=2)}
""".strip()


def load_domains():
    import yaml

    with (ROOT / "configs/domains.yaml").open() as f:
        cfg = yaml.safe_load(f)

    return cfg["domains"]


async def run(args):
    records = load_samples()
    taxonomy = load_domains()

    if args.limit:
        records = records[:args.limit]

    settings = GenerationSettings(
        temperature=0.0,
        max_output_tokens=3000,
        timeout=180,
        retries=1,
        seed=0,
    )

    if args.engine == "openai":
        engine = OpenAIChatEngine(model=args.model)
    else:
        engine = AnthropicEngine(model=args.model)

    RESULTS.mkdir(parents=True, exist_ok=True)

    model_hash = hashlib.sha256(args.model.encode()).hexdigest()[:10]
    output = RESULTS / f"{args.engine}_{model_hash}.jsonl"

    completed = set()
    if output.exists():
        for line in output.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("status") == "ok":
                    completed.add(row["record_id"])

    print(f"Model: {args.model}")
    print(f"Samples selected: {len(records)}")
    print(f"Already completed: {len(completed)}")
    print(f"Output: {output}")

    for index, record in enumerate(records, start=1):
        record_id = record["record_id"]

        if record_id in completed:
            continue

        try:
            response = await engine.generate(
                system=SYSTEM,
                user=build_prompt(record, taxonomy),
                schema=LLMExtraction,
                settings=settings,
            )

            raw = parse_json_response(response.content)
            prediction = LLMExtraction.model_validate(raw)

            row = {
                "record_id": record_id,
                "dataset": record["dataset"],
                "model": response.model,
                "engine": args.engine,
                "status": "ok",
                "prediction": prediction.model_dump(mode="json"),
                "usage": response.usage,
                "latency_s": response.latency_s,
                "attempts": response.attempts,
                "dropped_params": response.dropped_params,
                "structured_mode": response.structured_mode,
                "finish_reason": response.finish_reason,
                "raw_response": response.content,
            }

            completed.add(record_id)
            print(f"[{index}/{len(records)}] OK: {record_id}")

        except Exception as exc:
            row = {
                "record_id": record_id,
                "dataset": record["dataset"],
                "model": args.model,
                "engine": args.engine,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"[{index}/{len(records)}] ERROR: {record_id}: {exc}")

        with output.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["openai", "anthropic"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()