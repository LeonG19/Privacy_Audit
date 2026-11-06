"""Score pilot runs against the human reference sheet.

Inputs
    pilot_sample.json          which records were sampled (the shared contract)
    reference_annotations.csv  human reference: gold_<param> = value(s) separated by "|", or "<missing>" when the text
                               does not state it; empty = not annotated yet (skipped). gold_domain (+ gold_domain_alt,
                               "|"-separated acceptable alternatives); optional gold_privacy, used ONLY where filled in.
    judgments.csv              human verdicts (correct | partial | incorrect) for value pairs the scorer cannot decide.
                               The scorer appends new undecided pairs with an empty verdict and never edits a verdict.
    run directories            normal `extract` outputs (extractions.jsonl, failures.jsonl, run_manifest.json)

Per CI parameter each sampled record gets one outcome:
    correct | partial | incorrect | pending   reference states a value, model gave a value
    missed                                    reference states a value, model returned null
    correct_null                              reference says <missing>, model returned null
    invented                                  reference says <missing>, model returned a value
    failed                                    no valid output for the record (counts as wrong)
    benchmark_value                           value came from the benchmark, not the model (run without
                                              --blind-native-ci); excluded from accuracy
    unannotated                               reference cell empty; excluded
"""
from __future__ import annotations

import csv
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ci_audit.evaluation import gpu
from ci_audit.evaluation.sample import MISSING
from ci_audit.pipeline import PipelineError
from ci_audit.schemas import CI_PARAMS
from ci_audit.storage import archive_existing, read_json, read_jsonl, write_csv, write_json_atomic

VERDICTS = ("correct", "partial", "incorrect")
JUDGMENT_FIELDS = ["record_id", "dataset", "param", "model_value", "reference", "verdict", "token_f1", "models", "judged_by", "note"]
EXCLUDED = ("benchmark_value", "unannotated")

_ARTICLES = {"the", "a", "an"}
_PUNCT = re.compile(r"[^\w\s]")


# --------------------------------------------------------------------------- matching
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().replace("’", "'").replace("'", "")
    return " ".join(t for t in _PUNCT.sub(" ", s).split() if t not in _ARTICLES)


def token_f1(a: str, b: str) -> float:
    ta, tb = set(norm(a).split()), set(norm(b).split())
    inter = len(ta & tb)
    if not inter:
        return 0.0
    p, r = inter / len(ta), inter / len(tb)
    return 2 * p * r / (p + r)


def parse_gold(cell: Optional[str]):
    """None = not annotated; MISSING = not stated in the text; else the list of acceptable values."""
    cell = (cell or "").strip()
    if not cell:
        return None
    if cell.lower() == MISSING:
        return MISSING
    return [v.strip() for v in cell.split("|") if v.strip()] or None


def wilson(k: float, n: int, z: float = 1.96) -> Optional[list[float]]:
    """95% Wilson interval for k/n: with ~20 items per dataset, differences inside it are not evidence of anything."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


# --------------------------------------------------------------------------- inputs
def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _jkey(rid: str, param: str, value: str, reference: str) -> tuple:
    return rid, param, norm(value), norm(reference)


def _jrow_key(r: dict) -> tuple:
    return _jkey(*((r.get(k) or "") for k in ("record_id", "param", "model_value", "reference")))


def load_judgments(path: Path) -> dict[tuple, dict]:
    return {_jrow_key(r): r for r in read_csv_rows(path)}


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def load_run(run_dir: Path) -> dict:
    m = read_json(run_dir / "run_manifest.json")
    ok = {r["record_id"]: r for r in read_jsonl(run_dir / "extractions.jsonl")}
    fail = {r["record_id"]: r for r in read_jsonl(run_dir / "failures.jsonl") if r["record_id"] not in ok}
    priv = {r["record_id"]: r for r in read_jsonl(run_dir / "privacy_predictions.jsonl")}
    # A run done in batches (--resume) overwrites run_manifest.json each time; copies kept as run_manifest.<name>.json
    # (see scripts/run_pilot_vllm.sh) let time and GPU figures cover every session, not only the last one.
    found = {}
    for p in sorted(run_dir.glob("run_manifest*.json")):
        if not p.name.startswith("run_manifest_privacy"):
            x = read_json(p)
            found.setdefault(x["run_id"], x)
    sessions = sorted(([_ts(x["started_at"]), _ts(x["finished_at"]), x.get("records_resumed_from_previous") or 0] for x in found.values()))
    return {"dir": str(run_dir), "manifest": m, "dataset": m["dataset"], "engine": m["engine"]["engine"],
            "model": m["engine"]["model"], "blind": bool(m.get("blind_native_ci")), "ok": ok, "fail": fail, "privacy": priv,
            "sessions": [(s, e) for s, e, _ in sessions], "sessions_complete": sessions[0][2] == 0}


def system_name(run: dict) -> str:
    return f"{run['engine']}:{run['model']}"


# --------------------------------------------------------------------------- per-record outcomes
def ci_outcome(rid: str, param: str, gold, rec: Optional[dict], judgments: dict, auto_f1: float, pending: dict, sysname: str,
               dataset: str, gold_cell: str) -> str:
    if gold is None:
        return "unannotated"
    if rec is None:
        return "failed"
    p = rec["ci_parameters"][param]
    if p["origin"] in ("benchmark", "benchmark_mapped"):
        return "benchmark_value"
    val = p["value"]
    if gold == MISSING:
        return "correct_null" if val is None else "invented"
    if val is None:
        return "missed"
    if any(norm(val) == norm(g) or token_f1(val, g) >= auto_f1 for g in gold):
        return "correct"
    j = judgments.get(_jkey(rid, param, val, gold_cell))
    if j and j.get("verdict", "").strip().lower() in VERDICTS:
        return j["verdict"].strip().lower()
    key = _jkey(rid, param, val, gold_cell)
    row = pending.setdefault(key, {"record_id": rid, "dataset": dataset, "param": param, "model_value": val, "reference": gold_cell,
                                   "verdict": "", "token_f1": round(max(token_f1(val, g) for g in gold), 2), "models": set()})
    row["models"].add(sysname)
    return "pending"


def domain_outcome(ref: dict, rec: Optional[dict]) -> str:
    gold = (ref.get("gold_domain") or "").strip()
    if not gold:
        return "unannotated"
    if rec is None:
        return "failed"
    pred = rec["domain"]["domain"].strip().lower()
    if pred == gold.lower():
        return "correct"
    alts = [a.strip().lower() for a in (ref.get("gold_domain_alt") or "").split("|") if a.strip()]
    return "acceptable_alt" if pred in alts else "incorrect"


def param_metrics(c: Counter) -> dict:
    n = sum(v for k, v in c.items() if k not in EXCLUDED)
    decided = n - c["pending"]
    good = c["correct"] + c["correct_null"]
    stated = c["correct"] + c["partial"] + c["incorrect"] + c["pending"] + c["missed"]
    absent = c["correct_null"] + c["invented"]
    return {
        "n": n, "pending": c["pending"], "outcomes": dict(c),
        "accuracy_strict": round(good / decided, 3) if decided else None,
        "accuracy_strict_ci95": wilson(good, decided),
        "accuracy_lenient": round((good + c["partial"]) / decided, 3) if decided else None,
        "invention_rate": round(c["invented"] / absent, 3) if absent else None,  # value given where the text has none
        "miss_rate": round(c["missed"] / stated, 3) if stated else None,  # null where the text states one
    }


# --------------------------------------------------------------------------- efficiency / cost
def _subset_of(rid: str, refs: dict) -> str:
    return (refs.get(rid) or {}).get("subset") or "all"


def busy_intervals(recs: list[dict]) -> list[tuple[float, float]]:
    """Merged [start, end] of every request (processed_at minus its latency): time the model was actually working.

    Session wall time also counts idle gaps (a server restart, waiting between batches); these intervals do not.
    """
    iv = []
    for r in recs:
        meta = r.get("extraction_metadata") or r
        if meta.get("latency_s") is not None and meta.get("processed_at"):
            end = _ts(meta["processed_at"])
            iv.append((end - meta["latency_s"], end))
    merged: list[list[float]] = []
    for s, e in sorted(iv):
        if merged and s <= merged[-1][1] + 1:  # processed_at has 1 s resolution
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def efficiency(run: dict, ids: list[str], refs: dict, population: dict, pricing: dict, gpu_rows: list[dict],
               gpu_hourly: Optional[float]) -> dict:
    recs = [run["ok"].get(i) or run["fail"].get(i) for i in ids]
    recs = [r for r in recs if r is not None]

    def usage(r):
        return (r.get("extraction_metadata") or r).get("usage") or {}

    def latency(r):
        return (r.get("extraction_metadata") or r).get("latency_s")

    pt = [usage(r).get("prompt_tokens") for r in recs]
    ct = [usage(r).get("completion_tokens") for r in recs]
    lat = [x for r in recs if (x := latency(r)) is not None]
    m = run["manifest"]
    sessions = run.get("sessions") or [(_ts(m["started_at"]), _ts(m["finished_at"]))]
    wall = sum(e - s for s, e in sessions)
    busy_iv = busy_intervals(recs)
    busy = sum(e - s for s, e in busy_iv) or wall  # time spent on requests; falls back to wall time without timestamps
    complete_tokens = all(x is not None for x in pt + ct)
    out: dict[str, Any] = {
        "records": len(recs),
        "prompt_tokens": sum(pt) if complete_tokens else None,
        "completion_tokens": sum(ct) if complete_tokens else None,
        "latency_s_mean": round(statistics.mean(lat), 2) if lat else None,
        "latency_s_median": round(statistics.median(lat), 2) if lat else None,
        "latency_s_max": round(max(lat), 2) if lat else None,
        "wall_s": round(wall, 1),
        "busy_s": round(busy, 1),
        "concurrency": m.get("concurrency"),
        "sessions": len(sessions),
        "wall_time_partial": not run.get("sessions_complete", not m.get("records_resumed_from_previous")),
        "records_per_min": round(len(recs) / busy * 60, 2) if busy > 0 else None,
        "output_tokens_per_s": round(sum(ct) / busy, 1) if complete_tokens and busy > 0 else None,
    }
    # Full-dataset extrapolation per subset (trajectories cost far more than seeds), at the same concurrency.
    per_sub: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        per_sub[_subset_of(r["record_id"], refs)].append(r)
    def mean_of(rs, f):
        vals = [f(r) for r in rs]
        return None if not vals or any(v is None for v in vals) else statistics.mean(vals)

    est_pt = est_ct = est_lat = 0.0
    for sub, pop in population.items():
        rs = per_sub.get(sub)
        means = [mean_of(rs or [], f) for f in (lambda r: usage(r).get("prompt_tokens"), lambda r: usage(r).get("completion_tokens"), latency)]
        if any(m is None for m in means):  # a subset without (complete) pilot data: no estimate rather than an undercount
            est_pt = est_ct = est_lat = None
            break
        est_pt += pop * means[0]
        est_ct += pop * means[1]
        est_lat += pop * means[2]
    pilot_lat = sum(lat)
    est_wall = busy * est_lat / pilot_lat if est_lat is not None and pilot_lat else None
    out["full_dataset_estimate"] = {
        "records": sum(population.values()),
        "prompt_tokens": None if est_pt is None else round(est_pt),
        "completion_tokens": None if est_ct is None else round(est_ct),
        "wall_hours": round(est_wall / 3600, 2) if est_wall else None,
    }
    price = pricing.get(run["model"]) or {}
    if price.get("input_per_mtok") is not None and price.get("output_per_mtok") is not None and complete_tokens:
        cost = lambda p, c: round(p / 1e6 * price["input_per_mtok"] + c / 1e6 * price["output_per_mtok"], 4)
        out["api_cost_usd"] = cost(out["prompt_tokens"], out["completion_tokens"])
        if est_pt is not None:
            out["full_dataset_estimate"]["api_cost_usd"] = cost(est_pt, est_ct)
        out["priced_on"] = price.get("priced_on")
    if gpu_rows and run["engine"] == "vllm":
        g = gpu.summarize_windows(gpu_rows, busy_iv or sessions)
        if g:
            covered = sum(1 for s, e in busy_iv if any(s <= float(r["utc_epoch"]) <= e for r in gpu_rows))
            g["requests_window_coverage"] = f"{covered}/{len(busy_iv)} busy intervals have GPU samples" if busy_iv else None
        out["gpu"] = g
        if g:
            out["gpu_hours"] = round(g["n_gpus"] * busy / 3600, 3)
            if est_wall:
                out["full_dataset_estimate"]["gpu_hours"] = round(g["n_gpus"] * est_wall / 3600, 2)
            if gpu_hourly is not None:
                out["gpu_cost_usd"] = round(out["gpu_hours"] * gpu_hourly, 2)
                if est_wall:
                    out["full_dataset_estimate"]["gpu_cost_usd"] = round(g["n_gpus"] * est_wall / 3600 * gpu_hourly, 2)
    return out


def validity(run: dict, ids: list[str]) -> dict:
    ok = [run["ok"][i] for i in ids if i in run["ok"]]
    fail = [run["fail"][i] for i in ids if i in run["fail"]]
    meta = [r["extraction_metadata"] for r in ok]
    warn = Counter()
    for r in ok:
        for w in r["validation_warnings"]:
            if "evidence not found in source" in w or "not found in source" in w:
                warn["evidence_not_in_source"] += 1
            elif "DROPPED" in w:
                warn["dropped_unsupported_claim"] += 1
            elif "null without missing_reason" in w:
                warn["null_without_reason"] += 1
            elif "without evidence" in w:
                warn["value_without_evidence"] += 1
            else:
                warn["other"] += 1
    n = len(ids)
    return {
        "sampled": n, "ran": len(ok) + len(fail), "not_run": n - len(ok) - len(fail),
        "valid": len(ok), "valid_rate": round(len(ok) / n, 3) if n else None,
        "valid_first_try": sum(1 for x in meta if x["attempts"] == 1),
        "clean": sum(1 for x in meta if x["status"] == "ok"),
        "failed_by_type": dict(Counter(f["error_type"] for f in fail)),
        "structured_mode": dict(Counter(x.get("structured_output_mode") for x in meta)),
        "warnings": dict(warn),
        "records_with_unsupported_evidence": sum(1 for r in ok if any("not found in source" in w for w in r["validation_warnings"])),
    }


# --------------------------------------------------------------------------- main entry
def score(sample: dict, refs_rows: list[dict], runs: list[dict], judgments_path: Path, cfg: dict,
          gpu_rows: Optional[list[dict]] = None) -> dict:
    refs = {r["record_id"]: r for r in refs_rows}
    judgments = load_judgments(judgments_path)
    bad = [j for j in judgments.values() if (j.get("verdict") or "").strip() and (j.get("verdict") or "").strip().lower() not in VERDICTS]
    auto_f1 = float(cfg.get("scoring", {}).get("auto_accept_f1", 0.8))
    costs = cfg.get("costs") or {}
    pricing, gpu_hourly = costs.get("api_pricing") or {}, costs.get("gpu_hourly_usd")
    pending: dict[tuple, dict] = {}

    by_sys: dict[str, dict[str, dict]] = defaultdict(dict)
    warnings: list[str] = []
    if bad:
        warnings.append(f"{len(bad)} verdict(s) in {judgments_path.name} are not one of {'/'.join(VERDICTS)} and are treated as pending "
                        f"(e.g. {bad[0].get('verdict')!r} for {bad[0].get('record_id')} / {bad[0].get('param')})")
    for run in runs:
        if run["manifest"].get("is_mock"):
            warnings.append(f"{run['dir']}: mock-engine run ignored")
            continue
        ds = run["dataset"]
        if ds not in sample["datasets"]:
            warnings.append(f"{run['dir']}: dataset {ds} is not in the pilot sample; ignored")
            continue
        s = system_name(run)
        if ds in by_sys[s]:
            raise PipelineError(f"two runs for {s} on {ds}: {by_sys[s][ds]['dir']} and {run['dir']} (pass the run directories explicitly)")
        ids = sample["datasets"][ds]["record_ids"]
        foreign = set(run["ok"]) - set(ids)
        if foreign:
            warnings.append(f"{run['dir']}: {len(foreign)} record(s) outside the pilot sample are ignored")
        if not run["blind"] and any(p in r["native_ci"] for r in run["ok"].values() for p in CI_PARAMS):
            warnings.append(f"{s} on {ds}: run without --blind-native-ci, so benchmark-provided parameters are excluded from scoring")
        if not run.get("sessions_complete", not run["manifest"].get("records_resumed_from_previous")):
            warnings.append(f"{s} on {ds}: run was resumed and earlier sessions' manifests were not kept; wall time, throughput "
                            "and GPU figures cover only the sessions found")
        by_sys[s][ds] = run
    covered = {s: sorted(d) for s, d in by_sys.items()}
    if len({tuple(v) for v in covered.values()}) > 1:
        warnings.append(f"systems were run on different datasets {covered}; their all-dataset totals are not comparable")

    result: dict[str, Any] = {"systems": {}, "warnings": warnings}
    for s, per_ds in sorted(by_sys.items()):
        sysres: dict[str, Any] = {"datasets": {}, "all": {}}
        tot_ci: dict[str, Counter] = {p: Counter() for p in CI_PARAMS}
        tot_dom: Counter = Counter()
        tot_priv: Counter = Counter()
        for ds, run in sorted(per_ds.items()):
            ids = sample["datasets"][ds]["record_ids"]
            ci: dict[str, Counter] = {p: Counter() for p in CI_PARAMS}
            dom: Counter = Counter()
            priv: Counter = Counter()
            for rid in ids:
                ref = refs.get(rid, {})
                rec = run["ok"].get(rid)
                for p in CI_PARAMS:
                    cell = ref.get(f"gold_{p}", "")
                    ci[p][ci_outcome(rid, p, parse_gold(cell), rec, judgments, auto_f1, pending, s, ds, cell)] += 1
                dom[domain_outcome(ref, rec)] += 1
                gp = (ref.get("gold_privacy") or "").strip().lower()
                if gp:  # privacy appropriateness: scored only where a valid human reference exists
                    pred = run["privacy"].get(rid)
                    priv["no_prediction" if pred is None else ("correct" if pred["prediction"]["label"] == gp else "incorrect")] += 1
            for p in CI_PARAMS:
                tot_ci[p].update(ci[p])
            tot_dom.update(dom)
            tot_priv.update(priv)
            population = sample["datasets"][ds]["population"]
            sysres["datasets"][ds] = {
                "run_dir": run["dir"], "blind_native_ci": run["blind"],
                "ci": {p: param_metrics(ci[p]) for p in CI_PARAMS},
                "ci_overall": param_metrics(sum(ci.values(), Counter())),
                "domain": domain_metrics(dom),
                "privacy": priv_metrics(priv),
                "validity": validity(run, ids),
                "efficiency": efficiency(run, ids, refs, population, pricing, gpu_rows or [], gpu_hourly),
            }
        sysres["all"] = {
            "ci": {p: param_metrics(tot_ci[p]) for p in CI_PARAMS},
            "ci_overall": param_metrics(sum(tot_ci.values(), Counter())),
            "domain": domain_metrics(tot_dom),
            "privacy": priv_metrics(tot_priv),
            "efficiency": sum_efficiency([d["efficiency"] for d in sysres["datasets"].values()]),
        }
        result["systems"][s] = sysres
    result["pending_judgments"] = len(pending)
    merge_judgments(judgments_path, pending)
    result["annotation_coverage"] = {
        ds: sum(1 for rid in info["record_ids"] if any((refs.get(rid) or {}).get(f"gold_{p}") for p in CI_PARAMS))
        for ds, info in sample["datasets"].items()
    }
    return result


def domain_metrics(c: Counter) -> dict:
    n = sum(v for k, v in c.items() if k != "unannotated")
    return {
        "n": n, "outcomes": dict(c),
        "accuracy_strict": round(c["correct"] / n, 3) if n else None,
        "accuracy_strict_ci95": wilson(c["correct"], n),
        "accuracy_with_alternatives": round((c["correct"] + c["acceptable_alt"]) / n, 3) if n else None,
    }


def priv_metrics(c: Counter) -> Optional[dict]:
    n = sum(c.values())
    return {"n": n, "outcomes": dict(c), "accuracy": round(c["correct"] / n, 3)} if n else None


def sum_efficiency(parts: list[dict]) -> dict:
    def total(key, src=None):
        vals = [(p.get(src) or {}).get(key) if src else p.get(key) for p in parts]
        vals = [v for v in vals if v is not None]
        return round(sum(vals), 4) if vals and len(vals) == len(parts) else None

    return {
        "records": total("records"), "prompt_tokens": total("prompt_tokens"), "completion_tokens": total("completion_tokens"),
        "wall_s": total("wall_s"), "api_cost_usd": total("api_cost_usd"), "gpu_hours": total("gpu_hours"), "gpu_cost_usd": total("gpu_cost_usd"),
        "full_dataset_estimate": {k: total(k, "full_dataset_estimate") for k in
                                  ("records", "prompt_tokens", "completion_tokens", "wall_hours", "api_cost_usd", "gpu_hours", "gpu_cost_usd")},
    }


def merge_judgments(path: Path, pending: dict[tuple, dict]) -> None:
    """Append new undecided pairs; existing rows, verdicts and any extra columns a reviewer added are kept.

    The previous file is archived (`*.bak-<time>`) and the new one written atomically, so human work is never lost.
    """
    if not pending:
        return
    header: list[str] = []
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
    fields = header + [c for c in JUDGMENT_FIELDS if c not in header]
    existing = read_csv_rows(path)
    seen = {_jrow_key(r): r for r in existing}
    changed = False
    for key, row in sorted(pending.items()):
        if key in seen:
            have = set(filter(None, (m.strip() for m in (seen[key].get("models") or "").split(","))))
            merged = ", ".join(sorted(have | row["models"]))
            if merged != (seen[key].get("models") or ""):
                seen[key]["models"] = merged
                changed = True
        else:
            existing.append({**row, "models": ", ".join(sorted(row["models"]))})
            changed = True
    if not changed:
        return
    tmp = path.with_name(path.name + ".tmp")
    write_csv(tmp, [{k: ("" if v is None else v) for k, v in r.items() if k is not None} for r in existing], fields)
    archive_existing(path)
    tmp.replace(path)


# --------------------------------------------------------------------------- report
def _pct(x) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def _ci(x) -> str:
    return "" if not x else f" [{100 * x[0]:.0f}–{100 * x[1]:.0f}]"


def _num(x, nd=0) -> str:
    if x is None:
        return "–"
    return f"{x:,.{nd}f}"


def render_markdown(res: dict) -> str:
    L = ["# Pilot comparison report\n"]
    if res["pending_judgments"]:
        L.append(f"> **{res['pending_judgments']} value pair(s) await a human verdict in `judgments.csv`.** "
                 "They are excluded from accuracy until judged, so numbers below are provisional.\n")
    for w in res["warnings"]:
        L.append(f"> warning: {w}\n")
    L.append("\nAnnotated records per dataset: " + ", ".join(f"{k} {v}" for k, v in res["annotation_coverage"].items()) + "\n")
    systems = res["systems"]
    if not systems:
        return "".join(L) + "\nNo runs scored.\n"

    L.append("\n## CI parameter accuracy (all datasets)\n\nStrict accuracy (95% Wilson interval); partial verdicts count as wrong.\n\n")
    L.append("| system | " + " | ".join(CI_PARAMS) + " | overall |\n|---|" + "---|" * (len(CI_PARAMS) + 1) + "\n")
    for s, r in systems.items():
        cells = [f"{_pct(r['all']['ci'][p]['accuracy_strict'])}{_ci(r['all']['ci'][p]['accuracy_strict_ci95'])}" for p in CI_PARAMS]
        o = r["all"]["ci_overall"]
        L.append(f"| {s} | " + " | ".join(cells) + f" | {_pct(o['accuracy_strict'])}{_ci(o['accuracy_strict_ci95'])} |\n")

    L.append("\n## Missing information\n\nInvention = a value where the reference says the text states none. Miss = null where the text states one.\n\n")
    L.append("| system | " + " | ".join(f"{p} inv / miss" for p in CI_PARAMS) + " | records with unsupported evidence |\n|---|" + "---|" * (len(CI_PARAMS) + 1) + "\n")
    for s, r in systems.items():
        cells = [f"{_pct(r['all']['ci'][p]['invention_rate'])} / {_pct(r['all']['ci'][p]['miss_rate'])}" for p in CI_PARAMS]
        unsup = sum(d["validity"]["records_with_unsupported_evidence"] for d in r["datasets"].values())
        ran = sum(d["validity"]["valid"] for d in r["datasets"].values())
        L.append(f"| {s} | " + " | ".join(cells) + f" | {unsup}/{ran} |\n")

    datasets = sorted({ds for r in systems.values() for ds in r["datasets"]})
    L.append("\n## By dataset\n\nCI = overall strict accuracy over the five parameters; domain = strict (with accepted alternatives).\n\n")
    L.append("| dataset | " + " | ".join(f"{s} CI | {s} domain" for s in systems) + " |\n|---|" + "---|---|" * len(systems) + "\n")
    for ds in datasets:
        cells = []
        for s, r in systems.items():
            d = r["datasets"].get(ds)
            if not d:
                cells += ["not run", "not run"]
                continue
            cells.append(f"{_pct(d['ci_overall']['accuracy_strict'])}{_ci(d['ci_overall']['accuracy_strict_ci95'])}")
            cells.append(f"{_pct(d['domain']['accuracy_strict'])} ({_pct(d['domain']['accuracy_with_alternatives'])})")
        L.append(f"| {ds} | " + " | ".join(cells) + " |\n")

    L.append("\n## Domain classification (all datasets)\n\n| system | strict | with alternatives | n |\n|---|---|---|---|\n")
    for s, r in systems.items():
        d = r["all"]["domain"]
        L.append(f"| {s} | {_pct(d['accuracy_strict'])}{_ci(d['accuracy_strict_ci95'])} | {_pct(d['accuracy_with_alternatives'])} | {d['n']} |\n")

    L.append("\n## Output validity\n\n| system | dataset | valid | valid first try | clean (no warnings) | failures |\n|---|---|---|---|---|---|\n")
    for s, r in systems.items():
        for ds, d in r["datasets"].items():
            v = d["validity"]
            L.append(f"| {s} | {ds} | {v['valid']}/{v['sampled']} | {v['valid_first_try']} | {v['clean']} | {v['failed_by_type'] or '–'} |\n")

    L.append("\n## Time, tokens and cost\n\n| system | dataset | prompt tok | output tok | mean latency s | busy s | rec/min | out tok/s | API $ | GPU-h | GPU util | peak mem GiB |\n"
             "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for s, r in systems.items():
        for ds, d in r["datasets"].items():
            e = d["efficiency"]
            g = e.get("gpu") or {}
            L.append(f"| {s} | {ds} | {_num(e['prompt_tokens'])} | {_num(e['completion_tokens'])} | {_num(e['latency_s_mean'], 1)} | "
                     f"{_num(e['busy_s'])} | {_num(e['records_per_min'], 1)} | {_num(e['output_tokens_per_s'])} | {_num(e.get('api_cost_usd'), 2)} | "
                     f"{_num(e.get('gpu_hours'), 2)} | {_num(g.get('mean_util_pct'))}% | {_num(g.get('peak_mem_used_gib_all_gpus'), 1)} |\n")

    L.append("\n## Estimated cost of the full datasets\n\nPer-subset means from the pilot × population, same concurrency. "
             "Small samples: treat as order-of-magnitude.\n\n| system | records | prompt tok | output tok | wall h | API $ | GPU-h | GPU $ |\n|---|---|---|---|---|---|---|---|\n")
    for s, r in systems.items():
        f = r["all"]["efficiency"]["full_dataset_estimate"]
        L.append(f"| {s} | {_num(f['records'])} | {_num(f['prompt_tokens'])} | {_num(f['completion_tokens'])} | {_num(f['wall_hours'], 1)} | "
                 f"{_num(f['api_cost_usd'], 2)} | {_num(f['gpu_hours'], 1)} | {_num(f['gpu_cost_usd'], 2)} |\n")

    priv = [(s, r["all"]["privacy"]) for s, r in systems.items() if r["all"]["privacy"]]
    if priv:
        L.append("\n## Privacy appropriateness (separate; only records with a valid human reference)\n\n| system | accuracy | n |\n|---|---|---|\n")
        for s, p in priv:
            L.append(f"| {s} | {_pct(p['accuracy'])} | {p['n']} |\n")
    return "".join(L)


def write_report(res: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {"json": out_dir / "scores.json", "md": out_dir / "report.md"}
    write_json_atomic(files["json"], res)
    files["md"].write_text(render_markdown(res), encoding="utf-8")
    return files
