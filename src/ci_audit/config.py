"""Project-root discovery and YAML config loading."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def find_root(start: Path | None = None) -> Path:
    """Project root = $CI_AUDIT_ROOT, else the nearest ancestor of cwd (or this file) with configs/datasets.yaml."""
    env = os.environ.get("CI_AUDIT_ROOT")
    if env:
        return Path(env).resolve()
    for base in (start or Path.cwd(), Path(__file__).resolve()):
        for cand in [base, *base.parents]:
            if (cand / "configs" / "datasets.yaml").exists():
                return cand
    raise FileNotFoundError(
        "Cannot locate the project root (configs/datasets.yaml). Run from the project directory "
        "or set CI_AUDIT_ROOT."
    )


def load_env(root: Path) -> None:
    """Load .env without overriding variables already in the environment. Never prints values."""
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=False)
    except ImportError:  # pragma: no cover
        pass


@lru_cache(maxsize=None)
def _load_yaml(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(root: Path, name: str) -> dict:
    return _load_yaml(str(root / "configs" / f"{name}.yaml"))


class Paths:
    """All filesystem locations, derived from a root, so tests can point at a temp tree."""

    def __init__(self, root: Path, data_dir: Path | None = None):
        self.root = root
        self.data = data_dir or root / "data"
        self.raw = self.data / "raw"
        self.processed = self.data / "processed"
        self.manifests = self.data / "manifests"
        self.external = self.data / "external"
        self.prompts = root / "prompts"
