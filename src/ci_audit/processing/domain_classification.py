"""Domain handling: normalise model predictions against the (expandable) taxonomy and build the domain inventory."""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from ci_audit.prompts import Taxonomy
from ci_audit.schemas import DomainPrediction, FinalDomain
from ci_audit.storage import write_csv, write_json_atomic, utcnow


def finalize_domain(pred: DomainPrediction, taxonomy: Taxonomy) -> FinalDomain:
    """Canonicalise the label's case if it matches the taxonomy; never replace an out-of-taxonomy label."""
    label = pred.domain.strip()
    lookup = {n.lower(): n for n in taxonomy.names}
    reserved = {n.lower(): n for n in taxonomy.reserved}
    canon = lookup.get(label.lower()) or reserved.get(label.lower())
    in_tax = canon is not None
    reasons = []
    if pred.confidence == "low":
        reasons.append("low confidence")
    if pred.needs_review:
        reasons.append("model flagged for review")
    if (canon or label) == "Unclear":
        reasons.append("domain unclear")
    elif (canon or label) == "Other":
        reasons.append("domain 'Other' (candidate new domain)")
    elif not in_tax:
        reasons.append("domain not in taxonomy (candidate new domain)")
    if (canon or label) not in ("Unclear",) and not pred.supporting_span:
        reasons.append("no supporting span")
    data = pred.model_dump()
    data["domain"] = canon or label
    return FinalDomain(**data, in_taxonomy=in_tax, taxonomy_version=taxonomy.version, review_reasons=reasons)


def _is_uncertain(d: dict) -> bool:
    return d["confidence"] == "low" or d["domain"] == "Unclear"


def build_inventory(records: Iterable[dict], taxonomy: Taxonomy, examples_per_domain: int = 3) -> dict:
    """Aggregate ExtractionRecord dicts. Callers decide which runs to include (mock runs are excluded upstream)."""
    recs = list(records)
    by_domain: dict[str, dict] = {}
    datasets = sorted({r["dataset"] for r in recs})
    review = []
    for r in recs:
        d = r["domain"]
        e = by_domain.setdefault(
            d["domain"],
            {"domain": d["domain"], "in_taxonomy": d["in_taxonomy"], "n_total": 0, "n_uncertain": 0, "n_needs_review": 0,
             "by_dataset": Counter(), "subdomains": Counter(), "examples": []},
        )
        e["n_total"] += 1
        e["by_dataset"][r["dataset"]] += 1
        if _is_uncertain(d):
            e["n_uncertain"] += 1
        if d["subdomain"]:
            e["subdomains"][d["subdomain"]] += 1
        needs = bool(d["review_reasons"]) or d["needs_review"]
        if needs:
            e["n_needs_review"] += 1
            review.append(
                {"record_id": r["record_id"], "dataset": r["dataset"], "domain": d["domain"], "subdomain": d["subdomain"],
                 "confidence": d["confidence"], "reasons": "; ".join(d["review_reasons"]) or "model flagged",
                 "supporting_span": d["supporting_span"], "explanation": d["explanation"], "text": r["scenario_text"][:300]}
            )
        if d["supporting_span"] and len(e["examples"]) < examples_per_domain:
            e["examples"].append({"record_id": r["record_id"], "dataset": r["dataset"], "supporting_span": d["supporting_span"], "explanation": d["explanation"]})
    rows = sorted(by_domain.values(), key=lambda x: -x["n_total"])
    for e in rows:
        e["by_dataset"] = dict(e["by_dataset"])
        e["subdomains"] = dict(e["subdomains"].most_common(10))
    return {
        "generated_at": utcnow(),
        "taxonomy_version": taxonomy.version,
        "n_records": len(recs),
        "datasets": datasets,
        "domains": rows,
        "candidate_new_domains": [e["domain"] for e in rows if not e["in_taxonomy"] or e["domain"] == "Other"],
        "review_list": review,
    }


def write_inventory(inv: dict, out_dir: Path, stem: str = "domain_inventory") -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / f"{stem}.json", "csv": out_dir / f"{stem}.csv", "review": out_dir / f"{stem}_review_list.csv"}
    write_json_atomic(paths["json"], inv)
    ds = inv["datasets"]
    rows = []
    for e in inv["domains"]:
        row = {"domain": e["domain"], "in_taxonomy": e["in_taxonomy"], "n_total": e["n_total"], "n_uncertain": e["n_uncertain"], "n_needs_review": e["n_needs_review"]}
        for d in ds:
            row[f"n_{d}"] = e["by_dataset"].get(d, 0)
        row["top_subdomains"] = "; ".join(f"{k} ({v})" for k, v in list(e["subdomains"].items())[:5])
        row["example_evidence"] = " | ".join(f"[{x['record_id']}] {x['supporting_span']}" for x in e["examples"])
        rows.append(row)
    write_csv(paths["csv"], rows, ["domain", "in_taxonomy", "n_total", "n_uncertain", "n_needs_review", *[f"n_{d}" for d in ds], "top_subdomains", "example_evidence"])
    write_csv(paths["review"], inv["review_list"], ["record_id", "dataset", "domain", "subdomain", "confidence", "reasons", "supporting_span", "explanation", "text"])
    return paths
