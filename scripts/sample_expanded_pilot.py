"""Select 150 fresh records (50 ConfAIde, 50 PrivacyLens, 50 MPCI-Bench).

Run from repository root: python scripts/sample_expanded_pilot.py
Does not overwrite existing samples unless --overwrite is passed.
"""
import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ci_audit.config import Paths
from ci_audit.datasets import get_adapter_class

SEED = 20261106
PLAN = [
    ('confaide_tier2', ['2a'], 13),
    ('confaide_tier2', ['2b'], 12),
    ('confaide_tier3', ['all'], 25),
    ('privacylens', None, 50),
    ('mpci_bench', None, 50),
]


def read_jsonl(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seed', type=int, default=SEED)
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--extra-exclude', type=Path, action='append', default=[],
                   help='Additional JSONL files containing record_id (repeatable)')
    args = p.parse_args()
    paths = Paths(ROOT)
    old_pilot = paths.processed / 'pilot' / 'pilot_samples.jsonl'
    old_reference = paths.processed / 'pilot' / 'annotations' / 'pilot_reference_25.jsonl'
    if not old_pilot.exists():
        p.error(f'Required original pilot file missing: {old_pilot}')
    exclusions = [old_pilot, old_reference, *args.extra_exclude]
    # Exclude both exact record IDs and the underlying source IDs, so alternate
    # versions of previously used scenarios cannot leak into the new sample.
    excluded_record_ids = set()
    excluded_sources = set()
    for file in exclusions:
        for row in read_jsonl(file):
            rid = row.get('record_id')
            if rid:
                excluded_record_ids.add(rid)
                if ':' in rid:
                    excluded_sources.add(tuple(rid.split(':', 1)))
            dataset, source_id = row.get('dataset'), row.get('source_id')
            if dataset and source_id is not None:
                excluded_sources.add((str(dataset), str(source_id)))
    # Explicit safeguard for the five scenarios used in the focused experiment.
    excluded_record_ids.update({
        'confaide_tier2:2a-081', 'confaide_tier2:2b-094',
        'confaide_tier3:216', 'confaide_tier3:016', 'confaide_tier3:015',
    })
    excluded_sources.update((x.split(':', 1)[0], x.split(':', 1)[1]) for x in excluded_record_ids)

    rng = random.Random(args.seed)
    selected = []
    counts = Counter()
    used_source_ids = set()
    for dataset, subsets, count in PLAN:
        adapter = get_adapter_class(dataset)(paths, subsets=subsets)
        groups = {}
        for record in adapter.iter_records():
            key = (dataset, str(record.source_id))
            if record.record_id in excluded_record_ids or key in excluded_sources or key in used_source_ids:
                continue
            groups.setdefault(key, []).append(record)
        if len(groups) < count:
            raise RuntimeError(f'{dataset} {subsets}: need {count}, only {len(groups)} eligible unique source IDs')
        chosen = rng.sample(sorted(groups), count)
        for key in chosen:
            candidate = rng.choice(sorted(groups[key], key=lambda r: r.record_id))
            selected.append(candidate.model_dump(mode='json'))
            used_source_ids.add(key)
            counts[dataset] += 1
        print(f'{dataset} {subsets}: selected {count} / {len(groups)} eligible unique sources')

    ids = [r['record_id'] for r in selected]
    if len(selected) != 150 or len(ids) != len(set(ids)):
        raise AssertionError('Selection size or uniqueness check failed')
    if set(ids) & excluded_record_ids:
        raise AssertionError('Selected a previously excluded record')
    output_dir = paths.processed / 'expanded_pilot'
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / 'samples_150.jsonl'
    manifest = output_dir / 'sample_manifest.json'
    if not args.overwrite and (output.exists() or manifest.exists()):
        p.error('Output already exists. Use --overwrite only if intentionally resampling.')
    with output.open('w', encoding='utf-8') as f:
        for row in selected:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    manifest.write_text(json.dumps({
        'seed': args.seed, 'total': len(selected), 'counts': dict(counts),
        'plan': [{'dataset': d, 'subsets': s, 'count': n} for d, s, n in PLAN],
        'exclusion_files': [str(x) for x in exclusions],
        'excluded_record_count': len(excluded_record_ids),
        'record_ids': ids,
    }, indent=2), encoding='utf-8')
    print(f'\nSelected {len(selected)} NEW scenarios; dataset counts: {dict(counts)}')
    print(f'Samples: {output}\nManifest: {manifest}')


if __name__ == '__main__':
    main()
