"""Run 150-scenario expanded pilot. Dry-run by default; --execute calls API."""
import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from pydantic import BaseModel, Field
from ci_audit.engines.base import GenerationSettings
from ci_audit.engines.openai_api import OpenAIChatEngine
from ci_audit.schemas import LLMExtraction, CIParameter, strict_json_schema
from ci_audit.processing.validation import parse_json_response

SAMPLES = ROOT / 'data/processed/expanded_pilot/samples_150.jsonl'
RESULTS = ROOT / 'data/processed/expanded_pilot/results'

# Preserved verbatim from run_pilot_sam.py
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

# Added instructions only for ConfAIde, as agreed for the focused combined run.
CONFAIDE_SYSTEM = SYSTEM.replace(
    'Use only the provided scenario.',
    'Additionally, extract the three ConfAIde-specific parameters independently:\n'
    'information_type, actor, use.\n\n'
    'Keep the two parameter sets separate. Do not assume that ConfAIde\'s\n'
    'actor is equivalent to recipient or that use is equivalent to\n'
    'transmission_principle.\n\n'
    'Use only the provided scenario.'
)

class ConfAIdeParameters(BaseModel):
    information_type: CIParameter
    actor: CIParameter
    use: CIParameter

class CombinedExtraction(LLMExtraction):
    confaide_parameters: ConfAIdeParameters


def load_samples():
    with SAMPLES.open(encoding='utf-8') as f:
        records = [json.loads(line) for line in f if line.strip()]
    if len(records) != 150:
        raise ValueError(f'Expected 150 records; found {len(records)}')
    ids = [r['record_id'] for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate record IDs')
    counts = Counter('confaide' if r['dataset'].startswith('confaide') else r['dataset'] for r in records)
    if counts != {'confaide': 50, 'privacylens': 50, 'mpci_bench': 50}:
        raise ValueError(f'Unexpected distribution: {counts}')
    return records


def load_domains():
    import yaml
    with (ROOT / 'configs/domains.yaml').open() as f:
        return yaml.safe_load(f)['domains']


def build_prompt(record, taxonomy, schema):
    domains = '\n'.join(f'- {name}: {description}' for name, description in taxonomy.items())
    # Exact original prompt template from run_pilot_sam.py
    return f'''Classify the scenario using one of these domains:

{domains}

SCENARIO:
<source_material>
{record["scenario_text"]}
</source_material>

OUTPUT SCHEMA:
{json.dumps(strict_json_schema(schema), indent=2)}'''.strip()


def output_path(record):
    dataset = record['dataset']
    name = 'confaide_combined' if dataset.startswith('confaide') else dataset
    return RESULTS / f'{name}.jsonl'


def existing_ok(path):
    completed = set()
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get('status') == 'ok':
                    completed.add(row['record_id'])
    return completed


async def run(args):
    records = load_samples()
    taxonomy = load_domains()
    if args.limit is not None:
        records = records[:args.limit]
    print(f'Model: {args.model}; records in this run: {len(records)}')
    print(f'Execution: {"LIVE API" if args.execute else "DRY RUN (no API calls)"}')
    print('Prompts: ConfAIde=combined; PrivacyLens/MPCI-Bench=original')
    for name in ['confaide_combined', 'privacylens', 'mpci_bench']:
        print(f'Output: {RESULTS / (name + ".jsonl")}')
    if not args.execute:
        for r in records[:3]:
            schema = CombinedExtraction if r['dataset'].startswith('confaide') else LLMExtraction
            print(f'Preview: {r["record_id"]} -> {schema.__name__}')
        return
    settings = GenerationSettings(temperature=0.0, max_output_tokens=args.max_output_tokens,
                                  timeout=180, retries=1, seed=0)
    engine = OpenAIChatEngine(model=args.model)
    RESULTS.mkdir(parents=True, exist_ok=True)
    completed = {name: existing_ok(RESULTS / f'{name}.jsonl') for name in
                 ['confaide_combined', 'privacylens', 'mpci_bench']}
    for i, record in enumerate(records, 1):
        rid = record['record_id']
        is_conf = record['dataset'].startswith('confaide')
        name = 'confaide_combined' if is_conf else record['dataset']
        if rid in completed[name]:
            print(f'[{i}/{len(records)}] SKIP {rid}')
            continue
        schema = CombinedExtraction if is_conf else LLMExtraction
        system = CONFAIDE_SYSTEM if is_conf else SYSTEM
        try:
            response = await engine.generate(system=system,
                user=build_prompt(record, taxonomy, schema), schema=schema, settings=settings)
            raw = parse_json_response(response.content)
            prediction = schema.model_validate(raw)
            row = {'record_id': rid, 'dataset': record['dataset'], 'model': response.model,
                   'engine': 'openai', 'status': 'ok', 'prediction': prediction.model_dump(mode='json'),
                   'usage': response.usage, 'latency_s': response.latency_s,
                   'attempts': response.attempts, 'dropped_params': response.dropped_params,
                   'structured_mode': response.structured_mode, 'finish_reason': response.finish_reason,
                   'prompt_variant': 'confaide_combined' if is_conf else 'original',
                   'system_prompt_sha256': hashlib.sha256(system.encode()).hexdigest(),
                   'raw_response': response.content}
            completed[name].add(rid)
            print(f'[{i}/{len(records)}] OK {rid}')
        except Exception as exc:
            row = {'record_id': rid, 'dataset': record['dataset'], 'model': args.model,
                   'engine': 'openai', 'status': 'error', 'error': f'{type(exc).__name__}: {exc}',
                   'prompt_variant': 'confaide_combined' if is_conf else 'original'}
            print(f'[{i}/{len(records)}] ERROR {rid}: {exc}')
        with output_path(record).open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--max-output-tokens', type=int, default=4500)
    parser.add_argument('--execute', action='store_true', help='Actually make paid API calls')
    asyncio.run(run(parser.parse_args()))

if __name__ == '__main__':
    main()
