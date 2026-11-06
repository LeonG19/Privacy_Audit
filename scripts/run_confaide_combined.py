"""Extract ConfAIde's three native factors AND five standard CI fields in one call.

Run from the Privacy_Audit repository: python scripts/run_confaide_combined.py
"""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from ci_audit.engines.base import GenerationSettings
from ci_audit.engines.openai_api import OpenAIChatEngine
from ci_audit.schemas import CIParameter, LLMExtraction, strict_json_schema
from ci_audit.processing.validation import parse_json_response

SAMPLES = ROOT / 'data/processed/pilot/pilot_samples.jsonl'
REFERENCES = ROOT / 'data/processed/pilot/annotations/pilot_reference_25.jsonl'
RESULTS = ROOT / 'data/processed/pilot/results/confaide_combined'


class ConfAIdeParameters(BaseModel):
    information_type: CIParameter = Field(description='ConfAIde-specific information type: what information is involved.')
    actor: CIParameter = Field(description='ConfAIde-specific actor: the actor relevant to the benchmark scenario; identify its role using evidence, without assuming it is always sender or recipient.')
    use: CIParameter = Field(description='ConfAIde-specific use: what the information is used for or how it is used in the scenario.')


class CombinedExtraction(LLMExtraction):
    confaide_parameters: ConfAIdeParameters


# Preserve the original prompt; add a separate extraction task.
SYSTEM = """
You are a research assistant extracting Contextual Integrity parameters.

Extract all five parameters independently:
sender, subject, recipient, information_type, transmission_principle.

Additionally, extract the three ConfAIde-specific parameters independently:
information_type, actor, use.

Keep the two parameter sets separate. Do not assume that ConfAIde's
actor is equivalent to recipient or that use is equivalent to
transmission_principle.

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


def load_jsonl(path):
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def selected_samples():
    ids = [r['record_id'] for r in load_jsonl(REFERENCES)
           if r['record_id'].startswith('confaide_')]
    if len(ids) != 5 or len(set(ids)) != 5:
        raise ValueError(f'Expected 5 unique ConfAIde reference IDs, found {ids}')
    samples = {r['record_id']: r for r in load_jsonl(SAMPLES)}
    missing = set(ids) - set(samples)
    if missing:
        raise ValueError(f'Missing samples: {sorted(missing)}')
    return [samples[i] for i in ids]


def build_prompt(record, taxonomy):
    domains = '\n'.join(f'- {name}: {description}' for name, description in taxonomy.items())
    return f'''Classify the scenario using one of these domains:

{domains}

SCENARIO:
<source_material>
{record["scenario_text"]}
</source_material>

OUTPUT SCHEMA:
{json.dumps(strict_json_schema(CombinedExtraction), indent=2)}'''


async def run(args):
    import yaml
    with (ROOT / 'configs/domains.yaml').open(encoding='utf-8') as f:
        taxonomy = yaml.safe_load(f)['domains']
    records = selected_samples()
    settings = GenerationSettings(temperature=0.0, max_output_tokens=4500,
                                  timeout=180, retries=1, seed=0)
    RESULTS.mkdir(parents=True, exist_ok=True)
    model_hash = hashlib.sha256(args.model.encode()).hexdigest()[:10]
    output = RESULTS / f'openai_{model_hash}_confaide_combined.jsonl'
    completed = {r['record_id'] for r in load_jsonl(output) if r.get('status') == 'ok'} if output.exists() else set()
    print(f'Model: {args.model}\nSelected: {len(records)}\nAlready completed: {len(completed)}\nOutput: {output}')
    if args.dry_run:
        for record in records:
            print(record['record_id'])
        return
    engine = OpenAIChatEngine(model=args.model)
    for index, record in enumerate(records, 1):
        rid = record['record_id']
        if rid in completed:
            continue
        try:
            response = await engine.generate(system=SYSTEM,
                                             user=build_prompt(record, taxonomy),
                                             schema=CombinedExtraction, settings=settings)
            prediction = CombinedExtraction.model_validate(parse_json_response(response.content))
            row = {'record_id': rid, 'dataset': record['dataset'],
                   'model': response.model, 'engine': 'openai', 'status': 'ok',
                   'prediction': prediction.model_dump(mode='json'),
                   'usage': response.usage, 'latency_s': response.latency_s,
                   'attempts': response.attempts,
                   'dropped_params': response.dropped_params,
                   'structured_mode': response.structured_mode,
                   'finish_reason': response.finish_reason,
                   'raw_response': response.content}
            print(f'[{index}/{len(records)}] OK: {rid}')
        except Exception as exc:
            row = {'record_id': rid, 'dataset': record['dataset'],
                   'model': args.model, 'engine': 'openai', 'status': 'error',
                   'error': f'{type(exc).__name__}: {exc}'}
            print(f'[{index}/{len(records)}] ERROR: {rid}: {exc}')
        with output.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--dry-run', action='store_true')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
