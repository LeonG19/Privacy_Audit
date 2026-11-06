"""Run orchestration for `extract` and `classify-privacy`.

Output layout (per dataset / engine / model):
    <output-dir>/<dataset>/<engine>__<model-slug>/
        extractions.jsonl        successful ExtractionRecords          (extract)
        failures.jsonl           failed records with error + raw output (extract)
        extractions.csv          flat view for inspection
        privacy_predictions.jsonl / privacy_failures.jsonl             (classify-privacy)
        run_manifest.json        settings, counts, versions, timings
Results are appended and flushed one record at a time, so an interrupted run can be continued with --resume.
"""
from __future__ import annotations

import asyncio
import json
import platform
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ci_audit import __version__
from ci_audit.config import Paths, load_config
from ci_audit.datasets import get_adapter_class
from ci_audit.engines.base import Engine, GenerationSettings
from ci_audit.processing.ci_extraction import ExtractionFailure, extract_record
from ci_audit.processing.privacy_classification import classify_record
from ci_audit.prompts import PromptBuilder, load_taxonomy
from ci_audit.schemas import CI_PARAMS, SourceRecord
from ci_audit.storage import JsonlWriter, archive_existing, read_jsonl, slug, utcnow, write_csv, write_json_atomic

FATAL_ERRORS = {"AuthenticationFailed", "ModelNotFound"}


class PipelineError(RuntimeError):
    """User-facing problem (bad flags, existing outputs, changed prompt, ...)."""


@dataclass
class RunSpec:
    dataset: str
    engine: Engine
    settings: GenerationSettings
    output_dir: Path
    subsets: Optional[list[str]] = None
    limit: Optional[int] = None
    concurrency: int = 4
    validation_retries: int = 2
    resume: bool = False
    force: bool = False
    operation: str = "extract"  # extract | privacy
    record_ids: Optional[list[str]] = None  # restrict to these records (e.g. a shared pilot sample), in this order
    blind_native_ci: bool = False  # hide benchmark CI values from the model (evaluation); extract only
    prompt_variant: Optional[str] = None  # alternative template, configs/extraction.yaml key <dataset>_<variant>
    cli_args: dict[str, Any] = field(default_factory=dict)
    show_progress: bool = True


def run_dir(output_dir: Path, dataset: str, engine: str, model: str) -> Path:
    return output_dir / dataset / f"{engine}__{slug(model)}"


OPS = {
    "extract": {"ok": "extractions.jsonl", "fail": "failures.jsonl"},
    "privacy": {"ok": "privacy_predictions.jsonl", "fail": "privacy_failures.jsonl"},
}


# --------------------------------------------------------------------------- csv view
def _scalar(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def csv_row(r: dict) -> dict:
    row = {
        "record_id": r["record_id"], "dataset": r["dataset"], "subset": r.get("subset"), "level": r.get("level"), "source_id": r["source_id"],
        "scenario_text": r["scenario_text"], "native_reference_kind": r["native_reference"]["kind"], "native_label": _scalar(r["native_reference"]["label"]),
        "social_context": r["context"]["social_context"], "purpose": r["context"]["purpose"],
        "domain": r["domain"]["domain"], "subdomain": r["domain"]["subdomain"], "domain_confidence": r["domain"]["confidence"],
        "domain_in_taxonomy": r["domain"]["in_taxonomy"], "domain_needs_review": bool(r["domain"]["review_reasons"]),
        "domain_supporting_span": r["domain"]["supporting_span"], "n_warnings": len(r["validation_warnings"]),
        "status": r["extraction_metadata"]["status"], "model": r["extraction_metadata"]["model"], "engine": r["extraction_metadata"]["engine"],
    }
    for p in CI_PARAMS:
        row[p] = r["ci_parameters"][p]["value"]
        row[f"{p}_origin"] = r["ci_parameters"][p]["origin"]
        row[f"{p}_missing_reason"] = r["ci_parameters"][p]["missing_reason"]
    return row


CSV_FIELDS = (
    ["record_id", "dataset", "subset", "level", "source_id", "scenario_text", "native_reference_kind", "native_label"]
    + [c for p in CI_PARAMS for c in (p, f"{p}_origin", f"{p}_missing_reason")]
    + ["social_context", "purpose", "domain", "subdomain", "domain_confidence", "domain_in_taxonomy", "domain_needs_review",
       "domain_supporting_span", "n_warnings", "status", "model", "engine"]
)


# --------------------------------------------------------------------------- core
async def _execute(spec: RunSpec, records: list[SourceRecord], todo: list[SourceRecord], out_ok: Path, out_fail: Path,
                   worker: Callable[[SourceRecord], Awaitable[Any]], run_id: str) -> dict:
    sem = asyncio.Semaphore(max(1, spec.concurrency))
    ok_w, fail_w = JsonlWriter(out_ok), JsonlWriter(out_fail)
    counts = {"ok": 0, "failed": 0, "skipped_after_abort": 0}
    abort: dict[str, str] = {}
    bar = None
    if spec.show_progress and todo:
        from tqdm import tqdm

        bar = tqdm(total=len(todo), desc=f"{spec.dataset}", file=sys.stderr, unit="rec")

    async def one(rec: SourceRecord):
        async with sem:
            if abort:
                counts["skipped_after_abort"] += 1
                return
            try:
                result = await worker(rec)
                ok_w.write(result.model_dump(mode="json"))
                counts["ok"] += 1
            except ExtractionFailure as f:
                fail_w.write(f.failure.model_dump(mode="json"))
                counts["failed"] += 1
                if f.failure.error_type in FATAL_ERRORS:
                    abort["reason"] = f"{f.failure.error_type}: {f.failure.message}"
            finally:
                if bar:
                    bar.update(1)

    try:
        await asyncio.gather(*(one(r) for r in todo))
    finally:
        ok_w.close()
        fail_w.close()
        if bar:
            bar.close()
    if abort:
        raise PipelineError(f"run aborted: {abort['reason']} (completed results were kept; fix the problem and re-run with --resume)")
    return counts


def run(spec: RunSpec, paths: Paths) -> dict:
    return asyncio.run(run_async(spec, paths))


async def run_async(spec: RunSpec, paths: Paths) -> dict:
    root = paths.root
    ext_cfg = load_config(root, "extraction")
    taxonomy = load_taxonomy(root)
    builder = PromptBuilder(paths, ext_cfg, taxonomy)
    op = OPS[spec.operation]

    adapter = get_adapter_class(spec.dataset)(paths, spec.subsets)
    if spec.blind_native_ci and spec.operation != "extract":
        raise PipelineError("--blind-native-ci only applies to extract")
    if spec.operation == "privacy" and not adapter.privacy_classification_supported:
        raise PipelineError(f"privacy classification is only implemented for confaide_tier2 (not {spec.dataset}).")
    records = list(adapter.iter_records())
    if spec.record_ids is not None:
        by_id = {r.record_id: r for r in records}
        missing = [i for i in spec.record_ids if i not in by_id]
        if missing:
            raise PipelineError(f"{len(missing)} requested record id(s) not produced by the {spec.dataset} adapter with subsets "
                                f"{list(adapter.selected)} (e.g. {missing[:3]}); check --subset and the ids file")
        records = [by_id[i] for i in dict.fromkeys(spec.record_ids)]
    if spec.limit is not None:
        records = records[: spec.limit]
    if not records:
        raise PipelineError("the dataset adapter produced no records")

    # fail fast on credentials / unreachable server / unknown model (before any inference)
    await spec.engine.verify()

    rdir = run_dir(spec.output_dir, spec.dataset, spec.engine.name, spec.engine.model)
    out_ok, out_fail = rdir / op["ok"], rdir / op["fail"]
    key = builder.extraction_key(spec.dataset, spec.prompt_variant) if spec.operation == "extract" else "privacy_classification"
    cur_sha = builder.prompt_sha256(key, spec.blind_native_ci)

    existing = [p for p in (out_ok, out_fail) if p.exists() and p.stat().st_size > 0]
    done: set[str] = set()
    if existing:
        if spec.force:
            for p in existing:
                archive_existing(p)
        elif spec.resume:
            prev = list(read_jsonl(out_ok))
            shas = {r["extraction_metadata"]["prompt_sha256"] for r in prev}
            if shas and shas != {cur_sha}:
                raise PipelineError("the prompt templates changed since this run started (or --blind-native-ci differs); resuming would mix prompt versions. Use --force to start over (old files are archived).")
            done = {r["record_id"] for r in prev}
            archive_existing(out_fail)  # failed records are retried below; the old failure log is archived
        else:
            raise PipelineError(f"outputs already exist in {rdir}. Use --resume to continue or --force to start over (existing files are archived, never deleted).")

    todo = [r for r in records if r.record_id not in done]
    run_id = uuid.uuid4().hex[:12]
    t0 = utcnow()

    if spec.operation == "extract":
        async def worker(rec):
            return await extract_record(spec.engine, rec, builder, spec.settings, taxonomy, run_id, spec.validation_retries, spec.blind_native_ci,
                                        spec.prompt_variant)
    else:
        async def worker(rec):
            return await classify_record(spec.engine, rec, builder, spec.settings, run_id, spec.validation_retries)

    counts = await _execute(spec, records, todo, out_ok, out_fail, worker, run_id)

    # flat view + manifest
    all_ok = list(read_jsonl(out_ok))
    order = {r.record_id: i for i, r in enumerate(records)}
    all_ok.sort(key=lambda r: order.get(r["record_id"], 1 << 30))
    if spec.operation == "extract":
        write_csv(rdir / "extractions.csv", (csv_row(r) for r in all_ok), CSV_FIELDS)
    unresolved = [f["record_id"] for f in read_jsonl(out_fail)] if out_fail.exists() else []
    manifest = {
        "run_id": run_id,
        "operation": spec.operation,
        "dataset": spec.dataset,
        "subsets": list(adapter.selected),
        "adapter_status": adapter.status,
        "source_revision": adapter.source_revision(),
        "engine": spec.engine.describe(),
        "is_mock": spec.engine.is_mock,
        "generation": spec.settings.as_dict(),
        "concurrency": spec.concurrency,
        "validation_retries": spec.validation_retries,
        "prompt_version": builder.prompt_version(key, spec.blind_native_ci),
        "blind_native_ci": spec.blind_native_ci,
        "prompt_variant": spec.prompt_variant,
        "record_ids_requested": len(spec.record_ids) if spec.record_ids is not None else None,
        "prompt_sha256": cur_sha,
        "taxonomy_version": taxonomy.version,
        "limit": spec.limit,
        "records_selected": len(records),
        "records_resumed_from_previous": len(done),
        "records_attempted_this_run": len(todo),
        "ok_this_run": counts["ok"],
        "failed_this_run": counts["failed"],
        "total_ok_in_file": len(all_ok),
        "unresolved_failures": unresolved,
        "started_at": t0,
        "finished_at": utcnow(),
        "cli_args": spec.cli_args,
        "environment": {"ci_audit": __version__, "python": platform.python_version(), "platform": platform.platform()},
        "note": "Mock-engine output is test data, not extraction results." if spec.engine.is_mock else None,
    }
    write_json_atomic(rdir / f"run_manifest{'' if spec.operation == 'extract' else '_privacy'}.json", manifest)
    return {"run_dir": rdir, "manifest": manifest, "counts": counts}
