"""Download benchmark sources into data/raw/<source>/<part>/ and write provenance manifests.

Guarantees:
* Files are stored exactly as released (the `.git` / `.cache` bookkeeping dirs are the only things removed).
* A sha256 for every file is recorded in data/manifests/<source>.json; `verify_source` detects later edits.
* Failures are reported and written to <source>.last_error.json; a previously good copy is never overwritten
  by a failed attempt, and nothing is ever substituted.
* Unchanged sources (same resolved revision, files intact) are not re-downloaded unless force=True.
* Large external assets (MPCI-Bench's VISPR images) are never downloaded.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ci_audit.config import Paths
from ci_audit.storage import read_json, sha256_file, utcnow, write_json_atomic

SKIP_DIRS = {".git", ".cache"}


class DownloadError(RuntimeError):
    pass


# --------------------------------------------------------------------------- file helpers
def list_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            out.append(Path(dirpath) / fn)
    return out


def hash_tree(root: Path) -> list[dict]:
    return [
        {"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": sha256_file(p)}
        for p in list_files(root)
    ]


# --------------------------------------------------------------------------- git / hf backends
def _run_git(args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise DownloadError("git is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise DownloadError(f"git {args[0]} timed out after {timeout}s") from e
    if r.returncode != 0:
        raise DownloadError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:500]}")
    return r.stdout.strip()


def git_resolve(url: str, revision: str | None) -> str:
    if revision and len(revision) == 40:
        return revision
    out = _run_git(["ls-remote", url, revision or "HEAD"], timeout=60)
    if not out:
        raise DownloadError(f"Cannot resolve revision {revision or 'HEAD'!r} of {url}")
    return out.split()[0]


def git_fetch(url: str, revision: str, dest: Path) -> str:
    dest.mkdir(parents=True)
    _run_git(["init", "-q"], cwd=dest)
    _run_git(["remote", "add", "origin", url], cwd=dest)
    _run_git(["fetch", "-q", "--depth", "1", "origin", revision], cwd=dest)
    _run_git(["checkout", "-q", "FETCH_HEAD"], cwd=dest)
    resolved = _run_git(["rev-parse", "HEAD"], cwd=dest)
    shutil.rmtree(dest / ".git")
    return resolved


def hf_resolve(repo_id: str, revision: str | None) -> str:
    from huggingface_hub import HfApi

    try:
        return HfApi().dataset_info(repo_id, revision=revision).sha
    except Exception as e:  # network, 404, gated ...
        raise DownloadError(f"Cannot resolve Hugging Face dataset {repo_id}@{revision or 'main'}: {type(e).__name__}: {e}") from e


def hf_fetch(repo_id: str, revision: str, dest: Path) -> str:
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    from huggingface_hub import snapshot_download

    try:
        snapshot_download(repo_id=repo_id, repo_type="dataset", revision=revision, local_dir=str(dest))
    except Exception as e:
        raise DownloadError(f"Hugging Face download of {repo_id}@{revision} failed: {type(e).__name__}: {e}") from e
    shutil.rmtree(dest / ".cache", ignore_errors=True)
    return revision


# --------------------------------------------------------------------------- expected files
def check_expected(source_cfg: dict, part_dirs: dict[str, Path]) -> list[dict]:
    results = []
    for spec in source_cfg.get("expected_files", []):
        base = part_dirs.get(spec["part"])
        if base is None or not base.exists():
            results.append({"spec": spec, "found": 0, "ok": False, "detail": "part directory missing"})
            continue
        if "path" in spec:
            found = int((base / spec["path"]).is_file())
            results.append({"spec": spec, "found": found, "ok": found == 1})
        else:
            n = sum(1 for p in list_files(base) if fnmatch.fnmatch(p.relative_to(base).as_posix(), spec["glob"]))
            results.append({"spec": spec, "found": n, "ok": n >= spec.get("min_count", 1)})
    return results


# --------------------------------------------------------------------------- orchestration
def manifest_path(paths: Paths, source: str) -> Path:
    return paths.manifests / f"{source}.json"


def _is_unchanged(paths: Paths, source: str, resolved: dict[str, str]) -> bool:
    mp = manifest_path(paths, source)
    if not mp.exists():
        return False
    m = read_json(mp)
    if m.get("status") != "ok":
        return False
    for part, rev in resolved.items():
        if m.get("parts", {}).get(part, {}).get("resolved_revision") != rev:
            return False
    return not verify_source(paths, source)["problems"]


def verify_source(paths: Paths, source: str) -> dict:
    """Re-hash raw files against the manifest. Any difference is a violation of raw-data immutability."""
    mp = manifest_path(paths, source)
    if not mp.exists():
        return {"source": source, "problems": [f"no manifest at {mp}"], "files_checked": 0}
    m = read_json(mp)
    problems, n = [], 0
    for f in m.get("files", []):
        p = paths.raw / source / f["part"] / f["path"]
        n += 1
        if not p.exists():
            problems.append(f"missing: {f['part']}/{f['path']}")
        elif sha256_file(p) != f["sha256"]:
            problems.append(f"MODIFIED: {f['part']}/{f['path']}")
    for part in m.get("parts", {}):
        known = {f["path"] for f in m["files"] if f["part"] == part}
        pdir = paths.raw / source / part
        if pdir.exists():
            for p in list_files(pdir):
                if p.relative_to(pdir).as_posix() not in known:
                    problems.append(f"unexpected extra file: {part}/{p.relative_to(pdir).as_posix()}")
    return {"source": source, "problems": problems, "files_checked": n}


def download_source(
    source: str,
    source_cfg: dict,
    paths: Paths,
    force: bool = False,
    summarize: Callable[[str], dict] | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Download one source. Returns {"source", "status": ok|unchanged|failed, "manifest"|"error"}."""
    parts_cfg: dict[str, dict] = source_cfg["parts"]
    try:
        resolved: dict[str, str] = {}
        for part, pc in parts_cfg.items():
            if pc["kind"] == "git":
                resolved[part] = git_resolve(pc["url"], pc.get("revision"))
            elif pc["kind"] == "hf":
                resolved[part] = hf_resolve(pc["repo_id"], pc.get("revision"))
            else:
                raise DownloadError(f"unknown part kind {pc['kind']!r}")

        if not force and _is_unchanged(paths, source, resolved):
            log(f"  {source}: unchanged (revisions {', '.join(f'{k}@{v[:10]}' for k, v in resolved.items())}); use --force to re-download")
            return {"source": source, "status": "unchanged", "manifest": read_json(manifest_path(paths, source))}

        tmp_root = paths.raw / f".tmp-{source}"
        shutil.rmtree(tmp_root, ignore_errors=True)
        tmp_root.mkdir(parents=True)
        part_info: dict[str, Any] = {}
        try:
            for part, pc in parts_cfg.items():
                dest = tmp_root / part
                log(f"  {source}/{part}: downloading {pc.get('url') or pc.get('repo_id')} @ {resolved[part][:10]} ...")
                if pc["kind"] == "git":
                    rev = git_fetch(pc["url"], resolved[part], dest)
                    part_info[part] = {"kind": "git", "source_url": pc["url"]}
                else:
                    rev = hf_fetch(pc["repo_id"], resolved[part], dest)
                    part_info[part] = {"kind": "hf", "source_url": f"https://huggingface.co/datasets/{pc['repo_id']}"}
                part_info[part].update(requested_revision=pc.get("revision"), resolved_revision=rev)
            expected = check_expected(source_cfg, {p: tmp_root / p for p in parts_cfg})
            missing = [e for e in expected if not e["ok"]]
            if missing:
                raise DownloadError("expected files missing after download: " + "; ".join(str(e["spec"]) for e in missing))

            files = []
            for part in parts_cfg:
                for rec in hash_tree(tmp_root / part):
                    files.append({"part": part, **rec})
                part_info[part]["n_files"] = sum(1 for f in files if f["part"] == part)
                part_info[part]["bytes"] = sum(f["size"] for f in files if f["part"] == part)

            # Install: replace any older copy. A copy that no longer matches its manifest is kept, not deleted.
            final_root = paths.raw / source
            if final_root.exists():
                problems = verify_source(paths, source)["problems"] if manifest_path(paths, source).exists() else ["no manifest"]
                if problems:
                    keep = paths.raw / f".replaced-{source}-{datetime.now():%Y%m%dT%H%M%S}"
                    final_root.rename(keep)
                    log(f"  NOTE: previous {source} copy did not match its manifest ({problems[:2]}); kept at {keep}")
                else:
                    shutil.rmtree(final_root)
            tmp_root.rename(final_root)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        manifest = {
            "source": source,
            "description": source_cfg.get("description"),
            "status": "ok",
            "download_date": utcnow(),
            "parts": part_info,
            "expected_files_check": expected,
            "files": files,
            "external_assets": source_cfg.get("external_assets", {}),
            "adapters": {},
            "errors": [],
        }
        write_json_atomic(manifest_path(paths, source), manifest)  # written first so adapters can read revisions
        if summarize:
            for name in source_cfg.get("adapters", []):
                try:
                    manifest["adapters"][name] = summarize(name)
                except Exception as e:
                    manifest["adapters"][name] = {"error": f"{type(e).__name__}: {e}"}
                    manifest["status"] = "ok_with_adapter_errors"
            write_json_atomic(manifest_path(paths, source), manifest)
        err = paths.manifests / f"{source}.last_error.json"
        if err.exists():
            err.unlink()
        return {"source": source, "status": "ok", "manifest": manifest}

    except Exception as e:
        info = {"source": source, "attempted_at": utcnow(), "error_type": type(e).__name__, "error": str(e)[:2000]}
        write_json_atomic(paths.manifests / f"{source}.last_error.json", info)
        return {"source": source, "status": "failed", "error": f"{type(e).__name__}: {e}"}
