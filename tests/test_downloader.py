import subprocess
from pathlib import Path

import pytest

from ci_audit.downloader import download_source, hash_tree, manifest_path, verify_source
from ci_audit.storage import read_json


@pytest.fixture
def git_repo(tmp_path):
    """A tiny local git repo standing in for a benchmark repository (INVENTED content)."""
    repo = tmp_path / "upstream"
    (repo / "benchmark").mkdir(parents=True)
    (repo / "benchmark" / "data.txt").write_text("[TEST FIXTURE] line one\n[TEST FIXTURE] line two\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path)}
    for cmd in (["init", "-q"], ["config", "uploadpack.allowAnySHA1InWant", "true"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, env=env)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return repo, sha


def cfg(repo, sha=None, expect="benchmark/data.txt"):
    return {"description": "fixture", "parts": {"github": {"kind": "git", "url": str(repo), "revision": sha}},
            "expected_files": [{"part": "github", "path": expect}], "adapters": ["fx"]}


def test_manifest_records_provenance(paths, git_repo):
    repo, sha = git_repo
    r = download_source("fx", cfg(repo, sha), paths, summarize=lambda name: {"records": 2, "native_reference_kinds": ["none"]}, log=lambda s: None)
    assert r["status"] == "ok"
    m = read_json(manifest_path(paths, "fx"))
    assert m["parts"]["github"]["resolved_revision"] == sha and m["parts"]["github"]["source_url"] == str(repo)
    assert m["download_date"] and m["adapters"]["fx"]["records"] == 2 and m["status"] == "ok"
    assert m["expected_files_check"][0]["ok"] and m["files"][0]["path"] == "benchmark/data.txt" and len(m["files"][0]["sha256"]) == 64
    raw = paths.raw / "fx" / "github"
    assert (raw / "benchmark" / "data.txt").read_text().startswith("[TEST FIXTURE] line one") and not (raw / ".git").exists()
    assert verify_source(paths, "fx")["problems"] == []


def test_unchanged_source_is_not_redownloaded_unless_forced(paths, git_repo):
    repo, sha = git_repo
    logs = []
    download_source("fx", cfg(repo, sha), paths, log=logs.append)
    marker = paths.raw / "fx" / "github" / "marker"
    marker.write_text("x")  # an extra file; unchanged check must notice the raw tree is no longer pristine
    r = download_source("fx", cfg(repo, sha), paths, log=logs.append)
    assert r["status"] == "ok" and not marker.exists()  # tree did not match manifest -> re-fetched
    r2 = download_source("fx", cfg(repo, sha), paths, log=logs.append)
    assert r2["status"] == "unchanged"
    r3 = download_source("fx", cfg(repo, sha), paths, force=True, log=logs.append)
    assert r3["status"] == "ok"


def test_verify_detects_modified_raw_file(paths, git_repo):
    repo, sha = git_repo
    download_source("fx", cfg(repo, sha), paths, log=lambda s: None)
    (paths.raw / "fx" / "github" / "benchmark" / "data.txt").write_text("tampered")
    problems = verify_source(paths, "fx")["problems"]
    assert problems and problems[0].startswith("MODIFIED")


def test_missing_expected_file_is_a_reported_failure_and_installs_nothing(paths, git_repo):
    repo, sha = git_repo
    r = download_source("fx", cfg(repo, sha, expect="benchmark/nope.txt"), paths, log=lambda s: None)
    assert r["status"] == "failed" and "expected files missing" in r["error"]
    assert not (paths.raw / "fx").exists() and not manifest_path(paths, "fx").exists()
    assert (paths.manifests / "fx.last_error.json").exists()
    assert not list(paths.raw.glob(".tmp-*"))


def test_unreachable_source_fails_without_destroying_previous_good_copy(paths, git_repo, tmp_path):
    repo, sha = git_repo
    download_source("fx", cfg(repo, sha), paths, log=lambda s: None)
    before = hash_tree(paths.raw / "fx" / "github")
    r = download_source("fx", cfg(tmp_path / "does-not-exist", "a" * 40), paths, force=True, log=lambda s: None)
    assert r["status"] == "failed"
    assert hash_tree(paths.raw / "fx" / "github") == before and read_json(manifest_path(paths, "fx"))["status"] == "ok"


def test_adapter_errors_do_not_hide_the_download(paths, git_repo):
    repo, sha = git_repo

    def boom(name):
        raise ValueError("adapter could not parse")

    r = download_source("fx", cfg(repo, sha), paths, summarize=boom, log=lambda s: None)
    m = read_json(manifest_path(paths, "fx"))
    assert r["status"] == "ok" and m["status"] == "ok_with_adapter_errors" and "could not parse" in m["adapters"]["fx"]["error"]
