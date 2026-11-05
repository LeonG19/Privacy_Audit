"""Draw a reproducible extraction batch that excludes every record already processed elsewhere.

    python scripts/sample_batch.py --dataset airgapagent_r --dataset pisas --n 100 --seed 20261104 \
        --exclude pilot/pilot_sample.json --exclude data/processed/pilot/pilot_samples.jsonl \
        --out batch1/enrique_sample.json

Per dataset, `n` records are split over its subsets in proportion to their size (so the batch mirrors the dataset)
and drawn with a fixed seed. Excluded ids may come from a sample JSON ({"datasets": {name: {"record_ids": [...]}}}),
a JSONL of records with `record_id`, or a text file with one id per line. The output uses the pilot sample format,
so `extract --ids-file` reads it directly.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from ci_audit.config import Paths, find_root
from ci_audit.datasets import get_adapter_class
from ci_audit.storage import utcnow, write_json_atomic


def read_ids(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return {i for d in json.loads(text)["datasets"].values() for i in d["record_ids"]}
    if path.suffix == ".jsonl":
        return {json.loads(line)["record_id"] for line in text.splitlines() if line.strip()}
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def proportional(n: int, sizes: dict[str, int]) -> dict[str, int]:
    """Largest-remainder split of n over subsets by available size."""
    total = sum(sizes.values())
    raw = {s: n * k / total for s, k in sizes.items()}
    out = {s: int(v) for s, v in raw.items()}
    for s in sorted(raw, key=lambda s: raw[s] - out[s], reverse=True)[: n - sum(out.values())]:
        out[s] += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", action="append", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--exclude", type=Path, action="append", default=[])
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; refusing to overwrite a batch that may already have been run")

    paths = Paths(find_root())
    excluded = set().union(*(read_ids(p) for p in args.exclude)) if args.exclude else set()
    out = {"seed": args.seed, "n_per_dataset": args.n, "created_at": utcnow(),
           "excluded_from": [str(p) for p in args.exclude], "datasets": {}}
    for ds in args.dataset:
        adapter = get_adapter_class(ds)(paths)
        by_sub: dict[str, list[str]] = defaultdict(list)
        for r in adapter.iter_records():
            if r.record_id not in excluded:
                by_sub[r.subset or r.level or "all"].append(r.record_id)
        rng = random.Random(f"{args.seed}:{ds}")
        picked = []
        for sub, k in proportional(args.n, {s: len(v) for s, v in by_sub.items()}).items():
            pool = sorted(by_sub[sub])
            rng.shuffle(pool)
            picked += pool[:k]
        out["datasets"][ds] = {"subsets": list(adapter.selected), "source_revision": adapter.source_revision(),
                               "available_after_exclusion": {s: len(v) for s, v in sorted(by_sub.items())},
                               "record_ids": picked}
        print(f"{ds}: {len(picked)} drawn from {sum(len(v) for v in by_sub.values())} unprocessed records")
    write_json_atomic(args.out, out)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
