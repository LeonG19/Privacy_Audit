"""Create a reproducible 50-record pilot sample without modifying raw data."""

import json
import random
from pathlib import Path

from ci_audit.config import Paths, find_root
from ci_audit.datasets import get_adapter_class

SEED = 42

SAMPLE_PLAN = [
    ("confaide_tier2", ["2a"], 3),
    ("confaide_tier2", ["2b"], 2),
    ("confaide_tier3", ["all"], 5),
    ("privacylens", None, 10),
    ("mpci_bench", None, 10),
    ("airgapagent_r", ["full"], 10),
    ("pisas", None, 10),
]


def sample_records(paths, dataset, subsets, count, rng):
    adapter_class = get_adapter_class(dataset)
    adapter = adapter_class(paths, subsets=subsets)

    records = list(adapter.iter_records())

    # Group related records by their original source identifier.
    groups = {}

    for record in records:
        groups.setdefault(record.source_id, []).append(record)

    if len(groups) < count:
        raise ValueError(
            f"{dataset}: requested {count} samples, "
            f"but only {len(groups)} unique source IDs exist."
        )

    selected_ids = rng.sample(sorted(groups), count)

    selected = []

    for source_id in selected_ids:
        candidates = sorted(groups[source_id], key=lambda r: r.record_id)
        selected.append(rng.choice(candidates))

    return selected


def main():
    root = find_root()
    paths = Paths(root)
    rng = random.Random(SEED)

    selected = []

    for dataset, subsets, count in SAMPLE_PLAN:
        records = sample_records(paths, dataset, subsets, count, rng)

        for record in records:
            selected.append(record.model_dump(mode="json"))

        print(f"{dataset} ({subsets}): {len(records)} selected")

    output_dir = paths.processed / "pilot"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / "pilot_samples.jsonl"

    with output_file.open("w", encoding="utf-8") as f:
        for record in selected:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"\nTotal selected: {len(selected)}")
    print(f"Saved to: {output_file}")


if __name__ == "__main__":
    main()