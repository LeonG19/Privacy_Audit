"""Original samples: text + extracted CI parameters + domain, and selection of the subset to vary.

Known sources (searched automatically, all optional):
  * <processed>/expanded_pilot/results/*.jsonl  (+ samples_150.jsonl for the text)   ConfAIde / PrivacyLens / MPCI-Bench
  * <root>/batch1/runs/*/*/extractions.jsonl                                           AirGapAgent-R / PiSAs
  * any --extractions FILE.jsonl you pass (ExtractionRecord lines from `ci_audit extract`, or expanded-pilot result lines)

A subset file (--input) may be: a JSON/JSONL/text list of record ids, a pilot-style JSON
({"datasets": {name: {"record_ids": [...]}}}), or JSONL rows that already carry `scenario_text` + `ci_parameters`.
"""
from __future__ import annotations

import difflib
import json
import random
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional

from ci_audit.variation.knowledge import CI_PARAMS, DomainKnowledge

OTHER_DOMAIN = "Other services"   # what the manual review (domain_check.txt) uses for Other/Unclear


@dataclass
class Seed:
    record_id: str
    dataset: str
    text: str
    ci: dict[str, Optional[str]]
    domain: str
    domain_source: str                      # manual_review | model | fallback
    subset: Optional[str] = None
    level: Optional[str] = None
    origin_file: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers
def _jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _ci_values(ci_block: Optional[dict]) -> dict[str, Optional[str]]:
    vals: dict[str, Optional[str]] = {}
    for p in CI_PARAMS:
        v = (ci_block or {}).get(p)
        if isinstance(v, dict):
            v = v.get("value")
        v = v.strip() if isinstance(v, str) else None
        vals[p] = v or None
    return vals


def _model_domain(row: dict) -> Optional[str]:
    d = row.get("domain")
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        return d.get("domain")
    pred = row.get("prediction") or {}
    return (pred.get("domain") or {}).get("domain")


def resolve_domain(record_id: str, model_domain: Optional[str], kb: DomainKnowledge) -> tuple[str, str]:
    """Final domain: manual-review/knowledge-file assignment wins, then the model's label, else 'Other services'."""
    if record_id in kb.record_domain:
        return kb.record_domain[record_id], "manual_review"
    if model_domain and model_domain in kb.domains:
        return model_domain, "model"
    if model_domain in (None, "Other", "Unclear"):
        return OTHER_DOMAIN, "fallback"
    return model_domain, "model"     # a domain that has no pool; plan.py falls back to cross-domain values


# --------------------------------------------------------------------------- loading
def _from_extraction_record(row: dict, kb: DomainKnowledge, origin: str) -> Optional[Seed]:
    meta = row.get("extraction_metadata") or {}
    if meta.get("status", "ok") != "ok" or not row.get("ci_parameters"):
        return None
    text = row.get("scenario_text") or row.get("original_text")
    if not text:
        return None
    dom, src = resolve_domain(row["record_id"], _model_domain(row), kb)
    return Seed(row["record_id"], row["dataset"], text, _ci_values(row["ci_parameters"]), dom, src,
                row.get("subset"), row.get("level"), origin)


def _from_pilot_result(row: dict, samples: dict[str, dict], kb: DomainKnowledge, origin: str) -> Optional[Seed]:
    pred = row.get("prediction")
    s = samples.get(row["record_id"])
    if row.get("status") != "ok" or not pred or not s:
        return None
    dom, src = resolve_domain(row["record_id"], _model_domain(row), kb)
    return Seed(row["record_id"], row["dataset"], s["scenario_text"], _ci_values(pred["ci_parameters"]), dom, src,
                s.get("subset"), s.get("level"), origin)


def load_known_seeds(root: Path, processed: Path, kb: DomainKnowledge,
                     extra_files: Iterable[Path] = ()) -> dict[str, Seed]:
    """record_id -> Seed for every sample we have extracted CI parameters for."""
    seeds: dict[str, Seed] = {}
    pilot = processed / "expanded_pilot"
    samples: dict[str, dict] = {}
    sp = pilot / "samples_150.jsonl"
    if sp.exists():
        samples = {r["record_id"]: r for r in _jsonl(sp)}

    def add(seed: Optional[Seed]) -> None:
        if seed is not None:
            seeds.setdefault(seed.record_id, seed)

    for f in sorted((pilot / "results").glob("*.jsonl")) if (pilot / "results").exists() else []:
        for row in _jsonl(f):
            add(_from_pilot_result(row, samples, kb, str(f)))
    for f in sorted((root / "batch1" / "runs").glob("*/*/extractions.jsonl")):
        for row in _jsonl(f):
            add(_from_extraction_record(row, kb, str(f)))
    for f in extra_files:
        for row in _jsonl(Path(f)):
            if "prediction" in row:
                add(_from_pilot_result(row, samples, kb, str(f)))
            else:
                add(_from_extraction_record(row, kb, str(f)))
    return seeds


def read_subset_file(path: Path, known: dict[str, Seed], kb: DomainKnowledge) -> list[Seed]:
    """Resolve a user-supplied subset file to Seeds (see module docstring for accepted formats)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    ids: list[str] = []
    rows: list[dict] = []
    if path.suffix == ".json":
        obj = json.loads(text)
        if isinstance(obj, dict) and "datasets" in obj:
            for entry in obj["datasets"].values():
                ids.extend(entry.get("record_ids", []))
        elif isinstance(obj, list):
            for x in obj:
                (ids if isinstance(x, str) else rows).append(x)
        else:
            raise ValueError(f"{path}: unrecognised JSON layout (expected a list or a pilot sample with 'datasets')")
    elif path.suffix == ".jsonl":
        for r in _jsonl(path):
            if "scenario_text" in r or "ci_parameters" in r:
                rows.append(r)
            else:
                ids.append(r["record_id"])
    else:
        ids = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    out: list[Seed] = []
    missing: list[str] = []
    for rid in ids:
        if rid in known:
            out.append(known[rid])
        else:
            missing.append(rid)
    for r in rows:
        rid = r.get("record_id")
        if rid in known and not (r.get("scenario_text") and r.get("ci_parameters")):
            out.append(known[rid])
        elif r.get("scenario_text") and r.get("ci_parameters"):
            out.append(Seed(rid or f"custom:{len(out)}", r.get("dataset", "custom"), r["scenario_text"],
                            _ci_values(r["ci_parameters"]), *resolve_domain(rid or "", _model_domain(r), kb),
                            r.get("subset"), r.get("level"), str(path)))
        else:
            missing.append(str(rid))
    if missing:
        raise KeyError(
            f"{len(missing)} record id(s) in {path} have no extracted CI parameters/text in the known sources, e.g. "
            f"{missing[:3]}. Add the extraction file with --extractions, or put scenario_text + ci_parameters in the row.")
    return out


# --------------------------------------------------------------------------- selection
def match_domain(name: str, kb: DomainKnowledge) -> str:
    """Case-insensitive match of a user-given domain against the domains that have pools; helpful error otherwise."""
    by_lower = {d.lower(): d for d in kb.domains}
    if name.strip().lower() in by_lower:
        return by_lower[name.strip().lower()]
    hint = difflib.get_close_matches(name, kb.domains, n=1, cutoff=0.5)
    raise ValueError(f"unknown domain {name!r}. Domains with value pools: {', '.join(kb.domains)}."
                     + (f" Did you mean {hint[0]!r}?" if hint else ""))


def assign_domain(seeds: list[Seed], domain: str) -> list[Seed]:
    """Declare the domain of these samples yourself (e.g. a custom file); replacements are then drawn from that domain's pool."""
    return [replace(s, domain=domain, domain_source="user") for s in seeds]


def select_seeds(seeds: list[Seed], datasets: Optional[list[str]], per_dataset: Optional[int],
                 rng_seed: int, require_ci: bool = True, domains: Optional[list[str]] = None,
                 per_domain: Optional[int] = None, domains_per_dataset: Optional[int] = None) -> list[Seed]:
    """Filter by dataset and domain, drop samples without any extracted CI value, then sample (seeded, per dataset):

      per_dataset          N samples per dataset
      per_domain           N samples per (dataset, domain); only domains with at least N samples are eligible
      domains_per_dataset  M domains per dataset, drawn at random among the eligible ones (needs per_domain)

    Draws depend only on (rng_seed, dataset), so running datasets separately selects exactly what one combined run would.
    """
    pool = [s for s in seeds if (not datasets or s.dataset in datasets) and (not domains or s.domain in domains)]
    if require_ci:
        pool = [s for s in pool if any(s.ci.values())]
    by_ds: dict[str, list[Seed]] = {}
    for s in pool:
        by_ds.setdefault(s.dataset, []).append(s)
    chosen: list[Seed] = []
    for ds in sorted(by_ds):
        items = sorted(by_ds[ds], key=lambda s: s.record_id)
        if per_domain is not None:
            by_dom: dict[str, list[Seed]] = {}
            for s in items:
                by_dom.setdefault(s.domain, []).append(s)
            eligible = sorted(d for d, v in by_dom.items() if len(v) >= per_domain)
            if domains_per_dataset is not None and domains_per_dataset < len(eligible):
                eligible = sorted(random.Random(f"{rng_seed}|{ds}|domains").sample(eligible, domains_per_dataset))
            items = []
            for d in eligible:
                items.extend(sorted(random.Random(f"{rng_seed}|{ds}|{d}").sample(by_dom[d], per_domain), key=lambda s: s.record_id))
        elif per_dataset is not None and per_dataset < len(items):
            items = random.Random(f"{rng_seed}|{ds}").sample(items, per_dataset)
            items.sort(key=lambda s: s.record_id)
        chosen.extend(items)
    return chosen
