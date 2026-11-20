"""Command-line interface: python -m ci_audit <command> ..."""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Optional

from ci_audit.config import Paths, find_root, load_config, load_env
from ci_audit.datasets import ADAPTERS, get_adapter_class
from ci_audit.datasets.base import AdapterError
from ci_audit.downloader import download_source, manifest_path, verify_source
from ci_audit.engines import ENGINE_NAMES, create_engine
from ci_audit.engines.base import EngineError, GenerationSettings
from ci_audit.pipeline import PipelineError, RunSpec, run
from ci_audit.storage import read_jsonl, write_json_atomic


def _choice(options: list[str], what: str, extra: tuple[str, ...] = ()):
    allowed = list(options) + list(extra)

    def conv(value: str) -> str:
        if value in allowed:
            return value
        hint = difflib.get_close_matches(value, allowed, n=1)
        raise argparse.ArgumentTypeError(
            f"unknown {what} {value!r}. Choose from: {', '.join(allowed)}." + (f" Did you mean {hint[0]!r}?" if hint else "")
        )

    return conv


def build_parser(root: Optional[Path] = None) -> argparse.ArgumentParser:
    sources = ["confaide", "privacylens", "mpci_bench", "airgapagent_r", "pisas"]
    adapters = sorted(ADAPTERS)
    p = argparse.ArgumentParser(prog="ci_audit", description="Contextual Integrity benchmark audit toolkit")
    p.add_argument("--root", type=Path, help="project root (default: auto-detect from cwd or $CI_AUDIT_ROOT)")
    p.add_argument("--data-dir", type=Path, help="override <root>/data")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="download benchmark sources into data/raw and write provenance manifests")
    d.add_argument("--dataset", required=True, type=_choice(sources, "dataset", ("all",)))
    d.add_argument("--force", action="store_true", help="re-download even if unchanged")

    v = sub.add_parser("verify", help="re-hash data/raw against the manifests (raw data must be immutable)")
    v.add_argument("--dataset", default="all", type=_choice(sources, "dataset", ("all",)))

    c = sub.add_parser("check", help="run adapter loader checks (counts, label alignment, missing assets)")
    c.add_argument("--dataset", default="all", type=_choice(adapters, "dataset", ("all",)))

    sub.add_parser("list", help="list dataset adapters and their implementation status")

    def run_args(sp, privacy=False):
        sp.add_argument("--dataset", required=True, type=_choice(adapters, "dataset"))
        sp.add_argument("--engine", required=True, type=_choice(list(ENGINE_NAMES), "engine"), help="vllm | openai | anthropic (mock is for tests only)")
        sp.add_argument("--model", help="model name (required for openai/anthropic; for vllm it must be served - omitted: use the server's only model)")
        sp.add_argument("--base-url", help="API base URL (vllm default http://localhost:8000/v1)")
        sp.add_argument("--subset", help="comma-separated adapter subsets (see `list`)")
        sp.add_argument("--limit", type=int, help="process only the first N records")
        sp.add_argument("--ids-file", type=Path, help="process only these record ids: a pilot sample JSON (from `pilot sample`) or a text file with one id per line")
        sp.add_argument("--output-dir", type=Path, help="default: <data>/processed")
        sp.add_argument("--temperature", type=float)
        sp.add_argument("--max-output-tokens", type=int)
        sp.add_argument("--timeout", type=float, help="per-request timeout, seconds")
        sp.add_argument("--retries", type=int, help="transport retries (connection errors, 429, 5xx)")
        sp.add_argument("--validation-retries", type=int, help="extra attempts when the model's JSON fails validation")
        sp.add_argument("--concurrency", type=int)
        sp.add_argument("--seed", type=int)
        sp.add_argument("--resume", action="store_true", help="continue an interrupted run, skipping completed records")
        sp.add_argument("--force", action="store_true", help="start over; existing outputs are archived, not deleted")
        sp.add_argument("--no-progress", action="store_true")
        if not privacy:
            sp.add_argument("--prompt-variant", help="alternative extraction template, e.g. 'focused' (configs/extraction.yaml: <dataset>_<variant>)")
            sp.add_argument("--blind-native-ci", action="store_true",
                            help="EVALUATION: do not show benchmark-provided CI values; the model extracts all five (benchmark values stay in native_ci)")

    run_args(sub.add_parser("extract", help="extract CI parameters + domain for a dataset"))
    run_args(sub.add_parser("classify-privacy", help="OPTIONAL: model judges appropriate/inappropriate/uncertain (separate output file)"), privacy=True)

    vy = sub.add_parser("vary", help="create variations: replace ONE CI parameter per sample with a same-domain value and regenerate the story")
    vy.add_argument("--engine", required=True, type=_choice(["vllm", "openai", "anthropic"], "engine", extra=("mock",)),
                    help="LLM that writes the new stories: vllm | openai | anthropic")
    vy.add_argument("--model", help="model name (required for openai/anthropic; vllm: must be served, omitted = the server's only model)")
    vy.add_argument("--base-url", help="API base URL (vllm default http://localhost:8000/v1)")
    vy.add_argument("--input", type=Path, help="subset file: record ids (txt/json/jsonl, pilot-sample json) or jsonl rows with scenario_text + ci_parameters. Default: every known sample")
    vy.add_argument("--extractions", type=Path, action="append", default=[], help="extra extraction JSONL to take samples/CI parameters from (repeatable)")
    vy.add_argument("--datasets", help="comma-separated datasets to use (default: all that have extracted samples)")
    vy.add_argument("--domains", help="comma-separated domains: only use samples that belong to these domains (e.g. Healthcare,Education)")
    vy.add_argument("--domain", help="declare the domain of ALL selected samples (overrides the known assignment; use for custom --input files). "
                                     "Replacements are then drawn at random from this domain's pool")
    vy.add_argument("--per-domain", type=int, help="select N samples per (dataset, domain); only domains with at least N samples qualify")
    vy.add_argument("--domains-per-dataset", type=int, help="with --per-domain: use M randomly drawn domains per dataset (e.g. --per-domain 3 --domains-per-dataset 3)")
    vy.add_argument("--per-dataset", type=int, help="randomly select N samples per dataset (default: all)")
    vy.add_argument("--params", help="comma-separated CI parameters to replace (default: all five; null parameters are skipped)")
    vy.add_argument("--per-param", type=int, default=1, help="variants per (sample, parameter), each with a different replacement value")
    vy.add_argument("--seed", type=int, default=20261119, help="seed for sample selection and replacement draws")
    vy.add_argument("--max-pool-chars", type=int, default=200, help="ignore pool values longer than this")
    vy.add_argument("--strict-domain", action="store_true", help="never fall back to other domains when the domain pool has no replacement (skip instead)")
    vy.add_argument("--knowledge-md", type=Path, action="append", default=[], help="extra domain_knowledge.md-style file for the pools")
    vy.add_argument("--knowledge-csv", type=Path, action="append", default=[], help="extra norm_elements-style CSV for the pools")
    vy.add_argument("--output-dir", type=Path, help="default: <data>/processed/variations")
    vy.add_argument("--plan-only", action="store_true", help="only select samples and replacement values; no LLM call (writes plan.json)")
    vy.add_argument("--verify", action="store_true", help="second LLM call per variant that checks the new text against the original")
    vy.add_argument("--no-substitution", action="store_true", help="also use the LLM for structured 'seed' texts instead of replacing the line directly")
    vy.add_argument("--temperature", type=float, default=0.3)
    vy.add_argument("--max-output-tokens", type=int, default=6000)
    vy.add_argument("--timeout", type=float, default=300.0)
    vy.add_argument("--retries", type=int, default=3)
    vy.add_argument("--validation-retries", type=int, default=1)
    vy.add_argument("--concurrency", type=int, default=4)
    vy.add_argument("--resume", action="store_true")
    vy.add_argument("--force", action="store_true", help="start over; existing outputs are archived, not deleted")
    vy.add_argument("--no-progress", action="store_true")

    dm = sub.add_parser("domains", help="summarise extracted domains (CSV + JSON inventory)")
    dm.add_argument("--dataset", default="all", type=_choice(adapters, "dataset", ("all",)))
    dm.add_argument("--output-dir", type=Path, help="where extraction runs live (default: <data>/processed)")
    dm.add_argument("--run-dir", type=Path, action="append", help="explicit run directory (repeatable); default: newest non-mock run per dataset")
    dm.add_argument("--include-mock", action="store_true", help="include mock-engine test runs (never use for research)")

    pl = sub.add_parser("pilot", help="model-comparison pilot: shared sample + reference sheet, then scoring")
    psub = pl.add_subparsers(dest="pilot_command", required=True)
    ps = psub.add_parser("sample", help="draw the shared pilot sample (configs/pilot.yaml) and write the blank reference sheet")
    ps.add_argument("--out-dir", type=Path, default=Path("pilot"), help="default: pilot/")
    sc = psub.add_parser("score", help="score runs against the reference sheet; writes report.md + scores.json")
    sc.add_argument("run_dirs", nargs="*", type=Path, help="run directories; default: every run under <pilot-dir>/runs")
    sc.add_argument("--pilot-dir", type=Path, default=Path("pilot"), help="holds pilot_sample.json, reference_annotations.csv, judgments.csv")
    sc.add_argument("--gpu-log", type=Path, action="append", help="gpu-monitor CSV(s) covering the vLLM runs (repeatable)")
    sc.add_argument("--out-dir", type=Path, help="default: <pilot-dir>/reports")

    rb = psub.add_parser("review-batch", help="pick N records per dataset and write a sheet to judge one model's outputs by hand")
    rb.add_argument("--run-name", required=True, help="run folder under <pilot-dir>/runs/<dataset>/, e.g. vllm__llama-3.3-70b-instruct")
    rb.add_argument("--reviewer", required=True)
    rb.add_argument("--per-dataset", type=int, default=5)
    rb.add_argument("--seed", type=int, default=7)
    rb.add_argument("--prefer", type=Path, help="text file of record ids to include first (records other reviewers also judge)")
    rb.add_argument("--pilot-dir", type=Path, default=Path("pilot"))
    rs = psub.add_parser("review-summary", help="correct-judgment rates from filled review sheets")
    rs.add_argument("sheets", nargs="+", type=Path)

    gm = sub.add_parser("gpu-monitor", help="log nvidia-smi utilisation/memory/power with UTC timestamps until stopped")
    gm.add_argument("--out", type=Path, required=True)
    gm.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    return p


def _paths(args) -> Paths:
    root = (args.root or find_root()).resolve()
    load_env(root)
    return Paths(root, args.data_dir)


# --------------------------------------------------------------------------- commands
def cmd_download(args, paths: Paths) -> int:
    cfg = load_config(paths.root, "datasets")["sources"]
    names = list(cfg) if args.dataset == "all" else [args.dataset]
    paths.raw.mkdir(parents=True, exist_ok=True)
    paths.manifests.mkdir(parents=True, exist_ok=True)

    def summarize(adapter_name: str) -> dict:
        return get_adapter_class(adapter_name)(paths).summary()

    results = []
    for n in names:
        print(f"[{n}]")
        results.append(download_source(n, cfg[n], paths, force=args.force, summarize=summarize))
    print("\nDownload status:")
    bad = 0
    for r in results:
        if r["status"] == "failed":
            bad += 1
            print(f"  FAILED    {r['source']}: {r['error']}")
            continue
        m = r["manifest"]
        revs = ", ".join(f"{k}@{v['resolved_revision'][:10]}" for k, v in m["parts"].items())
        recs = ", ".join(f"{a}={s.get('records', 'ERR')}" for a, s in m["adapters"].items())
        print(f"  {r['status'].upper():9} {r['source']:14} {revs}  records: {recs}")
        for ex in m.get("external_assets", {}).values():
            print(f"            not downloaded (by design): {ex['reason']}")
    if bad:
        print(f"\n{bad} source(s) failed; nothing was substituted. See data/manifests/<source>.last_error.json.", file=sys.stderr)
    return 1 if bad else 0


def cmd_verify(args, paths: Paths) -> int:
    cfg = load_config(paths.root, "datasets")["sources"]
    names = list(cfg) if args.dataset == "all" else [args.dataset]
    bad = 0
    for n in names:
        r = verify_source(paths, n)
        if r["problems"]:
            bad += 1
            print(f"  {n}: PROBLEMS ({len(r['problems'])})")
            for x in r["problems"][:10]:
                print(f"     {x}")
        else:
            print(f"  {n}: OK ({r['files_checked']} files match the manifest)")
    return 1 if bad else 0


def cmd_check(args, paths: Paths) -> int:
    names = sorted(ADAPTERS) if args.dataset == "all" else [args.dataset]
    report, bad = {}, 0
    for n in names:
        a = get_adapter_class(n)(paths)
        try:
            r = a.check()
        except AdapterError as e:
            r = {"counts": {}, "problems": [str(e)], "notes": []}
        r["adapter_status"] = a.status
        report[n] = r
        flag = "PROBLEMS" if r["problems"] else "ok"
        print(f"[{n}] ({a.status}) {flag} counts={r['counts']}")
        for x in r["problems"]:
            print(f"    PROBLEM: {x}")
        for x in r["notes"]:
            print(f"    note: {x}")
        bad += bool(r["problems"])
    paths.manifests.mkdir(parents=True, exist_ok=True)
    write_json_atomic(paths.manifests / "loader_checks.json", report)
    return 1 if bad else 0


def cmd_list(args, paths: Paths) -> int:
    print(f"{'adapter':16} {'status':16} subsets (default)")
    for n, a in sorted(ADAPTERS.items()):
        print(f"{n:16} {a.status:16} {', '.join(a.subsets)}  ({', '.join(a.default_subsets)})")
    print("\nstatus: complete = extraction implemented and tested on real data; loader_verified = adapter reads the real release,")
    print("        extraction prompt not validated. Engines: " + ", ".join(ENGINE_NAMES[:3]))
    return 0


def _spec(args, paths: Paths, operation: str) -> RunSpec:
    cfg = load_config(paths.root, "extraction")
    g = cfg["generation"]
    pick = lambda v, k: g[k] if v is None else v
    settings = GenerationSettings(
        temperature=pick(args.temperature, "temperature"),
        max_output_tokens=pick(args.max_output_tokens, "max_output_tokens"),
        timeout=pick(args.timeout, "timeout"),
        retries=pick(args.retries, "retries"),
        seed=pick(args.seed, "seed"),
    )
    base_url = args.base_url
    ctk = None
    if args.engine == "vllm":
        base_url = base_url or cfg["engines"]["vllm"]["default_base_url"]
        ctk = cfg["engines"]["vllm"].get("chat_template_kwargs")
    engine = create_engine(args.engine, args.model, base_url=base_url, timeout=settings.timeout, chat_template_kwargs=ctk)
    return RunSpec(
        dataset=args.dataset,
        engine=engine,
        settings=settings,
        output_dir=(args.output_dir or paths.processed).resolve(),
        subsets=[s.strip() for s in args.subset.split(",")] if args.subset else None,
        limit=args.limit,
        concurrency=pick(args.concurrency, "concurrency"),
        validation_retries=pick(args.validation_retries, "validation_retries"),
        resume=args.resume,
        force=args.force,
        operation=operation,
        record_ids=read_ids_file(args.ids_file, args.dataset) if args.ids_file else None,
        blind_native_ci=getattr(args, "blind_native_ci", False),
        prompt_variant=getattr(args, "prompt_variant", None),
        cli_args={k: v for k, v in vars(args).items() if k not in ("root", "data_dir") and v is not None and not callable(v)} | {"base_url": base_url},
        show_progress=not args.no_progress,
    )


def read_ids_file(path: Path, dataset: str) -> list[str]:
    """Pilot sample JSON ({"datasets": {name: {"record_ids": [...]}}}) or plain text, one record id per line."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        entry = json.loads(text).get("datasets", {}).get(dataset)
        if entry is None:
            raise PipelineError(f"{path} has no record ids for dataset {dataset!r}")
        ids = entry["record_ids"]
    else:
        ids = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not ids:
        raise PipelineError(f"{path} lists no record ids for {dataset!r}")
    return ids


def cmd_run(args, paths: Paths, operation: str) -> int:
    if args.resume and args.force:
        raise PipelineError("--resume and --force are mutually exclusive")
    spec = _spec(args, paths, operation)
    if spec.engine.is_mock:
        print("NOTE: --engine mock produces content-free TEST output; it is not real extraction.", file=sys.stderr)
    res = run(spec, paths)
    m = res["manifest"]
    print(
        f"\n{operation}: {m['ok_this_run']} ok, {m['failed_this_run']} failed this run "
        f"({m['records_resumed_from_previous']} resumed from earlier) -> {res['run_dir']}"
    )
    if m["unresolved_failures"]:
        print(f"  {len(m['unresolved_failures'])} unresolved failure(s); see {res['run_dir']}/{'failures' if operation == 'extract' else 'privacy_failures'}.jsonl")
    if m["adapter_status"] != "complete":
        print(f"  NOTE: adapter '{spec.dataset}' is '{m['adapter_status']}': the extraction prompt has not been validated on this dataset.")
    return 0 if not m["unresolved_failures"] else 1


def cmd_vary(args, paths: Paths) -> int:
    import asyncio
    from collections import Counter

    from ci_audit.variation.generate import VariationRun, generate_variations
    from ci_audit.variation.knowledge import CI_PARAMS, DomainKnowledge
    from ci_audit.variation.plan import plan_variants
    from ci_audit.variation.seeds import assign_domain, load_known_seeds, match_domain, read_subset_file, select_seeds

    if args.resume and args.force:
        raise PipelineError("--resume and --force are mutually exclusive")
    params = tuple(x.strip() for x in args.params.split(",")) if args.params else CI_PARAMS
    bad = [x for x in params if x not in CI_PARAMS]
    if bad:
        raise PipelineError(f"unknown CI parameter(s) {bad}; choose from {list(CI_PARAMS)}")
    kb = DomainKnowledge.load(paths.root, paths.processed, args.knowledge_md, args.knowledge_csv)
    known = load_known_seeds(paths.root, paths.processed, kb, args.extractions)
    seeds = read_subset_file(args.input, known, kb) if args.input else list(known.values())
    datasets = [d.strip() for d in args.datasets.split(",")] if args.datasets else None
    if args.domain:
        seeds = assign_domain(seeds, match_domain(args.domain, kb))
    domains = [match_domain(d, kb) for d in args.domains.split(",")] if args.domains else None
    if args.domains_per_dataset and not args.per_domain:
        raise PipelineError("--domains-per-dataset needs --per-domain")
    if args.per_domain and args.per_dataset:
        raise PipelineError("use either --per-dataset or --per-domain, not both")
    seeds = select_seeds(seeds, datasets, args.per_dataset, args.seed, domains=domains,
                         per_domain=args.per_domain, domains_per_dataset=args.domains_per_dataset)
    if not seeds:
        raise PipelineError("no samples selected (check --input / --datasets; samples need extracted CI parameters)")
    plans, skipped = plan_variants(seeds, kb, args.seed, params, args.per_param, args.max_pool_chars, args.strict_domain)
    fallback = sum(p.pool_scope != "same_domain" for p in plans)
    print(f"samples: {len(seeds)} ({dict(Counter(s.dataset for s in seeds))})")
    print(f"domains: {dict(Counter(s.domain for s in seeds))}")
    if args.domains_per_dataset:
        for ds in sorted({s.dataset for s in seeds}):
            doms = sorted({s.domain for s in seeds if s.dataset == ds})
            print(f"  {ds}: {len(doms)} domain(s) {doms}" + ("   <-- fewer than requested" if len(doms) < args.domains_per_dataset else ""))
    print(f"variants planned: {len(plans)}  (by parameter: {dict(Counter(p.parameter for p in plans))})")
    print(f"  replacement drawn from the same domain: {len(plans) - fallback}; cross-domain fallback: {fallback}; skipped (empty pool): {len(skipped)}")

    cfg = load_config(paths.root, "extraction")
    base_url, ctk = args.base_url, None
    if args.engine == "vllm":
        base_url = base_url or cfg["engines"]["vllm"]["default_base_url"]
        ctk = cfg["engines"]["vllm"].get("chat_template_kwargs")
    out_dir = (args.output_dir or paths.processed / "variations").resolve()
    if args.plan_only:
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(out_dir / "plan.json", [
            {"variant_id": p.variant_id, "source_record_id": p.seed.record_id, "dataset": p.seed.dataset, "domain": p.seed.domain,
             "parameter": p.parameter, "original_value": p.original_value, "new_value": p.new_value,
             "pool_scope": p.pool_scope, "pool_size": p.pool_size} for p in plans])
        write_json_atomic(out_dir / "plan_skipped.json", skipped)
        print(f"plan only: wrote {out_dir / 'plan.json'} (no LLM calls)")
        return 0
    engine = create_engine(args.engine, args.model, base_url=base_url, timeout=args.timeout, chat_template_kwargs=ctk)
    if engine.is_mock:
        print("NOTE: --engine mock produces content-free TEST output.", file=sys.stderr)
    settings = GenerationSettings(temperature=args.temperature, max_output_tokens=args.max_output_tokens,
                                  timeout=args.timeout, retries=args.retries, seed=args.seed)
    run_cfg = VariationRun(engine=engine, settings=settings, out_dir=out_dir, concurrency=args.concurrency,
                           validation_retries=args.validation_retries, verify=args.verify, use_substitution=not args.no_substitution,
                           resume=args.resume, force=args.force, show_progress=not args.no_progress)
    extra = {"cli_args": {k: v for k, v in vars(args).items() if k not in ("root", "data_dir") and v is not None and not callable(v)},
             "knowledge_files": kb.files, "pool_sizes": kb.sizes(), "skipped_no_pool": skipped, "samples": len(seeds)}
    res = asyncio.run(generate_variations(plans, run_cfg, extra))
    m = res["manifest"]
    print(f"\nvary: {m['ok_this_run']} ok, {m['failed_this_run']} failed this run ({m['already_done_before_run']} resumed) -> {res['run_dir']}")
    if m["failed_this_run"]:
        print(f"  see {res['run_dir'] / 'failures.jsonl'}; rerun with --resume to retry them")
    return 0 if not m["failed_this_run"] else 1


def cmd_domains(args, paths: Paths) -> int:
    from ci_audit.processing.domain_classification import build_inventory, write_inventory
    from ci_audit.prompts import load_taxonomy

    out = (args.output_dir or paths.processed).resolve()
    runs: list[Path] = []
    if args.run_dir:
        runs = [r.resolve() for r in args.run_dir]
    else:
        names = sorted(ADAPTERS) if args.dataset == "all" else [args.dataset]
        for n in names:
            cands = [d for d in (out / n).glob("*") if (d / "extractions.jsonl").exists()] if (out / n).is_dir() else []
            if not args.include_mock:
                cands = [d for d in cands if not d.name.startswith("mock__")]
            if cands:
                newest = max(cands, key=lambda d: (d / "extractions.jsonl").stat().st_mtime)
                if len(cands) > 1:
                    print(f"  {n}: {len(cands)} runs found; using newest {newest.name} (pass --run-dir to choose)")
                runs.append(newest)
    if not runs:
        print("No extraction runs found. Run `python -m ci_audit extract ...` first.", file=sys.stderr)
        return 1
    records = []
    for r in runs:
        recs = list(read_jsonl(r / "extractions.jsonl"))
        if recs and recs[0]["extraction_metadata"]["is_mock"] and not args.include_mock:
            print(f"  skipping mock run {r}")
            continue
        records.extend(recs)
        print(f"  included {len(recs)} records from {r}")
    if not records:
        return 1
    inv = build_inventory(records, load_taxonomy(paths.root))
    inv["runs"] = [str(r) for r in runs]
    files = write_inventory(inv, out / "_inventory")
    print(f"\n{'domain':28} {'total':>6} {'uncertain':>9} {'review':>7}  in_taxonomy  by dataset")
    for e in inv["domains"]:
        print(f"{e['domain'][:28]:28} {e['n_total']:6d} {e['n_uncertain']:9d} {e['n_needs_review']:7d}  {str(e['in_taxonomy']):11}  {e['by_dataset']}")
    if inv["candidate_new_domains"]:
        print(f"\ncandidate new domains (outside taxonomy v{inv['taxonomy_version']}): {inv['candidate_new_domains']}")
    print(f"\n{len(inv['review_list'])} record(s) need human review.\nWrote: " + ", ".join(str(f) for f in files.values()))
    return 0


def _under_root(p: Path, paths: Paths) -> Path:
    return p if p.is_absolute() else paths.root / p


def cmd_pilot(args, paths: Paths) -> int:
    from ci_audit.evaluation import gpu, scoring
    from ci_audit.evaluation.sample import write_sample

    cfg = load_config(paths.root, "pilot")
    if args.pilot_command == "sample":
        files = write_sample(paths, cfg, _under_root(args.out_dir, paths))
        sample = json.loads(files["sample"].read_text(encoding="utf-8"))
        for ds, info in sample["datasets"].items():
            print(f"  {ds:16} {len(info['record_ids'])} records from {info['population']}")
        print("Wrote: " + ", ".join(str(f) for f in files.values()))
        print("Next: commit pilot_sample.json, then fill the gold_* columns of reference_annotations.csv (see docs/pilot_evaluation.md).")
        return 0
    if args.pilot_command == "review-summary":
        from ci_audit.evaluation import review

        print(review.render_summary(review.summarize(args.sheets)))
        return 0
    pdir = _under_root(args.pilot_dir, paths)
    sample = json.loads((pdir / "pilot_sample.json").read_text(encoding="utf-8"))
    if args.pilot_command == "review-batch":
        from ci_audit.evaluation import review

        refs = {r["record_id"]: r for r in scoring.read_csv_rows(pdir / "reference_annotations.csv")}
        prefer = set(read_ids_file(args.prefer, "") if args.prefer and args.prefer.suffix != ".json" else [])
        batch = review.pick_batch(sample, args.per_dataset, args.seed, prefer, {i: refs[i]["subset"] for i in refs})
        run_dirs = {ds: pdir / "runs" / ds / args.run_name for ds in batch}
        files = review.write_batch(run_dirs, batch, args.run_name, args.reviewer, pdir / "review")
        for ds, ids in batch.items():
            print(f"{ds}:")
            for i in ids:
                print(f"  {i}{'   (also in the other sample)' if i in prefer else ''}")
        print("Wrote: " + ", ".join(str(f) for f in files.values()))
        return 0
    runs = [r.resolve() for r in args.run_dirs] or sorted(m.parent for m in (pdir / "runs").glob("*/*/run_manifest.json")
                                                          if not m.parent.name.startswith("mock__"))
    if not runs:
        raise PipelineError(f"no run directories given and none found under {pdir / 'runs'}")
    gpu_rows = [row for log in (args.gpu_log or []) for row in gpu.read_log(log)]
    res = scoring.score(sample, scoring.read_csv_rows(pdir / "reference_annotations.csv"), [scoring.load_run(r) for r in runs],
                        pdir / "judgments.csv", cfg, gpu_rows)
    files = scoring.write_report(res, args.out_dir or pdir / "reports")
    print(files["md"].read_text(encoding="utf-8"))
    print("Wrote: " + ", ".join(str(f) for f in files.values()))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = _paths(args)
        if args.command == "download":
            return cmd_download(args, paths)
        if args.command == "verify":
            return cmd_verify(args, paths)
        if args.command == "check":
            return cmd_check(args, paths)
        if args.command == "list":
            return cmd_list(args, paths)
        if args.command == "extract":
            return cmd_run(args, paths, "extract")
        if args.command == "classify-privacy":
            return cmd_run(args, paths, "privacy")
        if args.command == "vary":
            return cmd_vary(args, paths)
        if args.command == "domains":
            return cmd_domains(args, paths)
        if args.command == "pilot":
            return cmd_pilot(args, paths)
        if args.command == "gpu-monitor":
            from ci_audit.evaluation.gpu import monitor

            return monitor(args.out, args.interval)
    except (PipelineError, AdapterError, EngineError, FileNotFoundError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as e:
        if args.command != "vary":
            raise
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted - completed records were saved; continue with --resume", file=sys.stderr)
        return 130
    parser.error("unknown command")
    return 2
