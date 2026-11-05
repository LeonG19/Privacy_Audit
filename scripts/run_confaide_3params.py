"""Run the five pre-annotated ConfAIde cases with a 3-field operational mapping.

Run from the Privacy_Audit repository, with its dependencies and API credentials set.
"""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from ci_audit.engines.base import GenerationSettings
from ci_audit.engines.openai_api import OpenAIChatEngine
from ci_audit.schemas import LLMExtraction, strict_json_schema
from ci_audit.processing.validation import parse_json_response

SAMPLES = ROOT / 'data/processed/pilot/pilot_samples.jsonl'
REFERENCES = ROOT / 'data/processed/pilot/annotations/pilot_reference_25.jsonl'
RESULTS = ROOT / 'data/processed/pilot/results/confaide_3params'

SYSTEM = '''You are a research assistant extracting three generalized factors from ConfAIde privacy scenarios.

For this experiment, use the following operational mapping:
1. Information Type -> ci_parameters.information_type: the information being disclosed or used.
2. Actor -> ci_parameters.recipient: the entity receiving the information. Do NOT return the sender merely because it performs the action.
3. Use -> ci_parameters.transmission_principle: how or for what purpose the information is used or disclosed. A stated purpose, sale, or other described use counts; it need not be a formal condition or permission.

These mappings are experimental conventions, not claims that the original ConfAIde authors defined these as exact CI equivalents.

Always set ci_parameters.sender and ci_parameters.subject to null. Do not extract them.
Use only the supplied scenario. Do not invent details. For each non-null field, include exact supporting evidence copied from the scenario. For null fields, use empty evidence arrays and a short missing_reason when the schema requires it.
Keep all required fields in the provided schema, including context and domain fields. Classify the social domain using the taxonomy. Do not judge privacy appropriateness.
Return JSON matching the supplied schema.'''


def load_jsonl(path):
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def selected_samples():
    refs = load_jsonl(REFERENCES)
    ids = [r['record_id'] for r in refs if r['record_id'].startswith('confaide_')]
    if len(ids) != 5 or len(set(ids)) != 5:
        raise ValueError(f'Expected exactly 5 unique ConfAIde references; found {ids}')
    samples = {r['record_id']: r for r in load_jsonl(SAMPLES)}
    missing = set(ids) - set(samples)
    if missing:
        raise ValueError(f'Missing scenario texts: {sorted(missing)}')
    return [samples[i] for i in ids]


def build_prompt(record, domains):
    domain_text = '\n'.join(f'- {k}: {v}' for k, v in domains.items())
    return f'''Classify the scenario using one of these domains:\n{domain_text}\n\nSCENARIO:\n<source_material>\n{record["scenario_text"]}\n</source_material>\n\nOUTPUT SCHEMA:\n{json.dumps(strict_json_schema(LLMExtraction), indent=2)}'''


async def run(args):
    import yaml
    with (ROOT / 'configs/domains.yaml').open(encoding='utf-8') as f:
        domains = yaml.safe_load(f)['domains']
    records = selected_samples()
    settings = GenerationSettings(temperature=0.0, max_output_tokens=3000,
                                  timeout=180, retries=1, seed=0)
    engine = OpenAIChatEngine(model=args.model)
    RESULTS.mkdir(parents=True, exist_ok=True)
    model_hash = hashlib.sha256(args.model.encode()).hexdigest()[:10]
    output = RESULTS / f'openai_{model_hash}_confaide_3params.jsonl'
    completed = {r['record_id'] for r in load_jsonl(output) if r.get('status') == 'ok'} if output.exists() else set()
    print(f'Model: {args.model}; selected: {len(records)}; completed: {len(completed)}; output: {output}')
    if args.dry_run:
        for r in records:
            print(f'{r["record_id"]}: {r["scenario_text"]}')
        return
    for index, record in enumerate(records, 1):
        rid = record['record_id']
        if rid in completed:
            continue
        try:
            response = await engine.generate(system=SYSTEM, user=build_prompt(record, domains),
                                             schema=LLMExtraction, settings=settings)
            prediction = LLMExtraction.model_validate(parse_json_response(response.content))
            params = prediction.ci_parameters
            for name in ('sender', 'subject'):
                if getattr(params, name).value is not None:
                    raise ValueError(f'{name} must be null under 3-parameter protocol')
            row = {'record_id': rid, 'dataset': record['dataset'], 'model': response.model,
                   'engine': 'openai', 'status': 'ok', 'prediction': prediction.model_dump(mode='json'),
                   'usage': response.usage, 'latency_s': response.latency_s,
                   'attempts': response.attempts, 'dropped_params': response.dropped_params,
                   'structured_mode': response.structured_mode, 'finish_reason': response.finish_reason,
                   'raw_response': response.content}
            print(f'[{index}/5] OK: {rid}')
        except Exception as exc:
            row = {'record_id': rid, 'dataset': record['dataset'], 'model': args.model,
                   'engine': 'openai', 'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
            print(f'[{index}/5] ERROR: {rid}: {exc}')
        with output.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--dry-run', action='store_true')
    asyncio.run(run(parser.parse_args()))

if __name__ == '__main__':
    main()
