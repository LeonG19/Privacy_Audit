"""Pick the pilot records once (seeded, stratified by subset) and write the human reference sheet.

The sample file is the contract between the people comparing models: every run uses `--ids-file` on it, and the
reference sheet is keyed by the same record ids. Re-running `sample` refuses to overwrite either file.
"""
from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path

from ci_audit.config import Paths
from ci_audit.datasets import get_adapter_class
from ci_audit.schemas import CI_PARAMS, SourceRecord
from ci_audit.storage import utcnow, write_csv, write_json_atomic

# Links that tie records to the same underlying scenario (levels of one case, a positive/negative pair, the
# 2a/2b rewrite of one flow). At most one record per group is drawn so the pilot covers distinct scenarios.
_GROUP_LINKS = ("case_id", "pair_id", "label_index")

MISSING = "<missing>"  # reference value: the text does not state this parameter
REFERENCE_FIELDS = (
    ["record_id", "dataset", "subset", "scenario_chars"]
    + [f"bench_{p}" for p in CI_PARAMS] + ["bench_domain"]
    + [f"gold_{p}" for p in CI_PARAMS] + ["gold_domain", "gold_domain_alt", "gold_privacy", "annotator", "notes"]
)


def _group(rec: SourceRecord) -> str:
    for k in _GROUP_LINKS:
        if rec.links.get(k) is not None:
            return f"{k}={rec.links[k]}"
    return rec.record_id


def quotas(n: int, subsets: list[str]) -> dict[str, int]:
    base, extra = divmod(n, len(subsets))
    return {s: base + (i < extra) for i, s in enumerate(subsets)}


def select_dataset(paths: Paths, dataset: str, subsets: list[str], n: int, seed: int) -> tuple[list[SourceRecord], dict]:
    adapter = get_adapter_class(dataset)(paths, subsets)
    by_subset: dict[str, list[SourceRecord]] = defaultdict(list)
    for r in adapter.iter_records():
        by_subset[r.subset or r.level or "all"].append(r)
    rng = random.Random(f"{seed}:{dataset}")
    used: set[str] = set()
    picked: list[SourceRecord] = []
    for sub, q in quotas(n, list(adapter.selected)).items():
        pool = sorted(by_subset.get(sub, []), key=lambda r: r.record_id)
        rng.shuffle(pool)
        take: list[SourceRecord] = []
        for r in pool:
            if len(take) == q:
                break
            if _group(r) not in used:
                used.add(_group(r))
                take.append(r)
        if len(take) < q:
            raise ValueError(f"{dataset}/{sub}: only {len(take)} distinct records available, {q} requested")
        picked.extend(take)
    info = {
        "subsets": list(adapter.selected),
        "source_revision": adapter.source_revision(),
        "population": {s: len(v) for s, v in sorted(by_subset.items())},
        "record_ids": [r.record_id for r in picked],
        "content_sha256": {r.record_id: r.content_sha256 for r in picked},
    }
    return picked, info


def build_sample(paths: Paths, cfg: dict) -> tuple[dict, list[SourceRecord]]:
    out = {"sample_version": cfg["sample_version"], "seed": cfg["seed"], "n_per_dataset": cfg["n_per_dataset"],
           "created_at": utcnow(), "datasets": {}}
    records: list[SourceRecord] = []
    for name, d in cfg["datasets"].items():
        recs, info = select_dataset(paths, name, d["subsets"], cfg["n_per_dataset"], cfg["seed"])
        out["datasets"][name] = info
        records.extend(recs)
    return out, records


def reference_rows(records: list[SourceRecord]) -> list[dict]:
    """Blank `gold_*` columns for humans; `bench_*` shows what the benchmark itself states (a hint, not the answer)."""
    rows = []
    for r in records:
        row = {"record_id": r.record_id, "dataset": r.dataset, "subset": r.subset or r.level, "scenario_chars": len(r.scenario_text)}
        for p in CI_PARAMS:
            row[f"bench_{p}"] = r.native_ci[p].value if p in r.native_ci else ""
        row["bench_domain"] = r.native_domain or ""
        rows.append(row)
    return rows


def scenarios_markdown(records: list[SourceRecord]) -> str:
    """The exact text each model sees (scenario_text), numbered to match the reference sheet."""
    out = ["# Pilot scenarios\n", "Exactly the `scenario_text` shown to every model. Annotate in `reference_annotations.csv`.\n"]
    for i, r in enumerate(records, 1):
        out.append(f"\n## {i}. `{r.record_id}`  ({r.dataset}, {r.subset or r.level})\n")
        if r.native_ci:
            out.append("Benchmark-provided CI (hint only; verify against the text): "
                       + "; ".join(f"{k} = {v.value}" for k, v in r.native_ci.items()) + "\n")
        out.append("\n```text\n" + r.scenario_text.replace("```", "'''") + "\n```\n")
    return "".join(out)


def write_sample(paths: Paths, cfg: dict, out_dir: Path) -> dict[str, Path]:
    files = {"sample": out_dir / "pilot_sample.json", "reference": out_dir / "reference_annotations.csv",
             "scenarios": out_dir / "pilot_scenarios.md"}
    clash = [str(p) for k, p in files.items() if k != "scenarios" and p.exists()]
    if clash:
        raise FileExistsError(f"refusing to overwrite {clash}: the sample and reference sheet are shared, possibly annotated. "
                              "Move them away first if you really mean to draw a new sample (and bump sample_version).")
    sample, records = build_sample(paths, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(files["sample"], sample)
    write_csv(files["reference"], reference_rows(records), REFERENCE_FIELDS)
    files["scenarios"].write_text(scenarios_markdown(records), encoding="utf-8")
    return files
