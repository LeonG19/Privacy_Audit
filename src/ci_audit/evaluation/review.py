"""Human review of model outputs when there is no ground truth: judge each extracted item, then count.

A review sheet has one row per (record, item), item = one of the five CI parameters or `domain`. The reviewer reads the
scenario and the model's value (with its evidence) and writes a judgment:

    correct     the value is right (for a null: the text really does not state it)
    partial     right party / type, but too broad, too narrow or with an extra wrong piece
    incorrect   wrong value
    invented    a value the text does not support (hallucination)
    missed      null, but the text does state it

`summarize` turns filled sheets (several reviewers / models allowed) into correct-judgment rates.
"""
from __future__ import annotations

import csv
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from ci_audit.evaluation.sample import quotas
from ci_audit.schemas import CI_PARAMS
from ci_audit.storage import read_jsonl, write_csv

ITEMS = (*CI_PARAMS, "domain")
JUDGMENTS = ("correct", "partial", "incorrect", "invented", "missed")
SHEET_FIELDS = ["record_id", "dataset", "subset", "model", "item", "model_value", "model_evidence", "model_missing_reason",
                "judgment", "reviewer", "note"]


def pick_batch(sample: dict, per_dataset: int, seed: int, prefer: set[str], subsets_of: dict[str, str]) -> dict[str, list[str]]:
    """Per dataset: `prefer`red ids first (records another reviewer also judges), then a seeded pick spread over subsets."""
    rng = random.Random(seed)
    out = {}
    for ds, info in sample["datasets"].items():
        ids = info["record_ids"]
        chosen = [i for i in ids if i in prefer][:per_dataset]
        by_sub: dict[str, list[str]] = defaultdict(list)
        for i in ids:
            if i not in chosen:
                by_sub[subsets_of[i]].append(i)
        need = quotas(per_dataset, info["subsets"])
        for i in chosen:  # preferred records count against their subset's quota
            need[subsets_of[i]] = max(0, need.get(subsets_of[i], 0) - 1)
        for sub in info["subsets"]:
            pool = sorted(by_sub[sub])
            rng.shuffle(pool)
            chosen += pool[: need.get(sub, 0)]
        rest = sorted(i for i in ids if i not in chosen)  # a subset short of records: fill from the others
        rng.shuffle(rest)
        out[ds] = (chosen + rest)[:per_dataset]
    return out


def sheet_rows(record: dict, model: str) -> list[dict]:
    rows = []
    for item in ITEMS:
        if item == "domain":
            d = record["domain"]
            value = d["domain"] + (f" / {d['subdomain']}" if d.get("subdomain") else "")
            evidence, reason = d.get("supporting_span") or "", d.get("explanation") or ""
        else:
            p = record["ci_parameters"][item]
            value, evidence, reason = p["value"], " | ".join(p["evidence"]), p["missing_reason"] or ""
        rows.append({"record_id": record["record_id"], "dataset": record["dataset"], "subset": record.get("subset") or record.get("level"),
                     "model": model, "item": item, "model_value": "<null>" if value is None else value,
                     "model_evidence": evidence, "model_missing_reason": reason})
    return rows


def review_markdown(records: list[dict], model: str, reviewer: str) -> str:
    out = [f"# Review batch: {model} ({reviewer})\n\n",
           "Judge each item in the CSV sheet: correct | partial | incorrect | invented | missed (see docs/pilot_evaluation.md).\n"]
    for n, r in enumerate(records, 1):
        out.append(f"\n## {n}. `{r['record_id']}` ({r['dataset']}, {r.get('subset') or r.get('level')})\n\n")
        out.append("```text\n" + r["scenario_text"].replace("```", "'''") + "\n```\n\n")
        out.append("| item | model value | evidence / reason |\n|---|---|---|\n")
        for row in sheet_rows(r, model):
            ev = row["model_evidence"] or row["model_missing_reason"]
            out.append(f"| {row['item']} | {row['model_value']} | {ev.replace('|', '/')[:300]} |\n")
        if r.get("validation_warnings"):
            out.append("\nValidator warnings: " + "; ".join(w[:160] for w in r["validation_warnings"]) + "\n")
    return "".join(out)


def write_batch(run_dirs: dict[str, Path], batch: dict[str, list[str]], model: str, reviewer: str, out_dir: Path) -> dict[str, Path]:
    """One CSV to fill in + one Markdown page to read, in the batch order."""
    records = []
    for ds, ids in batch.items():
        by_id = {r["record_id"]: r for r in read_jsonl(run_dirs[ds] / "extractions.jsonl")}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError(f"{ds}: no model output for {missing}")
        records += [by_id[i] for i in ids]
    stem = f"{reviewer}_{model}".replace("/", "-").replace(":", "-")
    files = {"sheet": out_dir / f"{stem}.csv", "page": out_dir / f"{stem}.md"}
    if files["sheet"].exists():
        raise FileExistsError(f"refusing to overwrite {files['sheet']} (it may already hold judgments)")
    write_csv(files["sheet"], [row for r in records for row in sheet_rows(r, model)], SHEET_FIELDS)
    files["page"].write_text(review_markdown(records, model, reviewer), encoding="utf-8")
    return files


def summarize(sheets: list[Path]) -> dict:
    """Correct-judgment rates per model, overall / per item / per dataset. Unjudged rows are counted, not scored."""
    counts: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    problems: list[str] = []
    for path in sheets:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                j = (row.get("judgment") or "").strip().lower()
                if j and j not in JUDGMENTS:
                    problems.append(f"{path.name}: {row['record_id']} / {row['item']}: unknown judgment {j!r}")
                    j = ""
                key = j or "unjudged"
                m = row["model"]
                for group in ("all", f"item:{row['item']}", f"dataset:{row['dataset']}"):
                    counts[m][group][key] += 1
    return {"models": {m: {g: rates(c) for g, c in groups.items()} for m, groups in counts.items()}, "problems": problems}


def rates(c: Counter) -> dict:
    judged = sum(v for k, v in c.items() if k != "unjudged")
    pct = lambda k: round(c[k] / judged, 3) if judged else None
    return {"judged": judged, "unjudged": c["unjudged"], "correct_rate": pct("correct"),
            "correct_or_partial_rate": round((c["correct"] + c["partial"]) / judged, 3) if judged else None,
            "invented": c["invented"], "missed": c["missed"], "counts": dict(c)}


def render_summary(s: dict) -> str:
    L = ["# Review summary (human judgments of model outputs)\n"]
    for p in s["problems"]:
        L.append(f"> {p}\n")
    for m, groups in sorted(s["models"].items()):
        a = groups["all"]
        L.append(f"\n## {m}\n\nOverall: {a['judged']} items judged ({a['unjudged']} still unjudged), correct {_pct(a['correct_rate'])}, "
                 f"correct or partial {_pct(a['correct_or_partial_rate'])}, invented {a['invented']}, missed {a['missed']}\n\n")
        L.append("| group | judged | correct | correct or partial | invented | missed |\n|---|---|---|---|---|---|\n")
        for g in [f"item:{i}" for i in ITEMS] + sorted(k for k in groups if k.startswith("dataset:")):
            if g in groups:
                r = groups[g]
                L.append(f"| {g.split(':', 1)[1]} | {r['judged']} | {_pct(r['correct_rate'])} | {_pct(r['correct_or_partial_rate'])} | "
                         f"{r['invented']} | {r['missed']} |\n")
    return "".join(L)


def _pct(x: Optional[float]) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"
