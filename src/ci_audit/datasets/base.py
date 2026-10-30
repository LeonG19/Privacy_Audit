"""Dataset adapter base class.

Adapters are read-only views over data/raw/. They never write to raw files, never call an LLM, and
never invent labels, images, or CI values. Anything the benchmark does not state is simply absent.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator, Optional

from ci_audit.config import Paths
from ci_audit.schemas import SourceRecord
from ci_audit.storage import read_json, sha256_text


class AdapterError(RuntimeError):
    """Raised when raw data is missing or does not have the expected structure."""


class DatasetAdapter(ABC):
    name: str = ""
    source: str = ""  # key in configs/datasets.yaml `sources`
    subsets: tuple[str, ...] = ()
    default_subsets: tuple[str, ...] = ()
    status: str = "unimplemented"  # complete | loader_verified | unimplemented
    privacy_classification_supported = False

    def __init__(self, paths: Paths, subsets: Optional[list[str]] = None, source_revision: Optional[str] = None):
        self.paths = paths
        self.raw = paths.raw / self.source
        chosen = tuple(subsets) if subsets else self.default_subsets
        bad = [s for s in chosen if s not in self.subsets]
        if bad:
            raise AdapterError(f"Unknown subset(s) {bad} for {self.name}. Available: {', '.join(self.subsets)}")
        self.selected = chosen
        self._revision = source_revision

    # ------------------------------------------------------------------ provenance
    def source_revision(self) -> str:
        if self._revision:
            return self._revision
        mp = self.paths.manifests / f"{self.source}.json"
        if not mp.exists():
            return "unknown (no manifest; run `python -m ci_audit download`)"
        parts = read_json(mp)["parts"]
        return "; ".join(f"{p}@{info['resolved_revision']}" for p, info in parts.items())

    def require(self, *rel: str) -> Path:
        """Return raw/<rel> or raise a helpful error pointing at the download command."""
        p = self.raw.joinpath(*rel)
        if not p.is_file():
            raise AdapterError(
                f"{self.name}: missing raw file {p.relative_to(self.paths.data.parent) if self.paths.data.parent in p.parents else p}. "
                f"Run: python -m ci_audit download --dataset {self.source}"
            )
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.paths.raw).as_posix()

    @staticmethod
    def digest(*parts: str) -> str:
        return sha256_text("\x1f".join(parts))

    # ------------------------------------------------------------------ interface
    @abstractmethod
    def iter_records(self) -> Iterator[SourceRecord]:
        ...

    def check(self) -> dict:
        """Loader sanity checks. Returns {"counts":..., "problems": [...], "notes": [...]} - problems are never swallowed."""
        recs = list(self.iter_records())
        ids = [r.record_id for r in recs]
        problems = []
        if len(ids) != len(set(ids)):
            problems.append("duplicate record_id values")
        warned = [r.record_id for r in recs if r.parse_warnings]
        if warned:
            problems.append(f"{len(warned)} records with parse warnings (e.g. {warned[0]})")
        return {"counts": {"records": len(recs)}, "problems": problems, "notes": []}

    def summary(self) -> dict:
        """Used in the download manifest: number of records and available labels."""
        recs = list(self.iter_records())
        kinds = sorted({r.native_reference.kind for r in recs})
        return {"records": len(recs), "native_reference_kinds": kinds, "subsets": list(self.selected), "status": self.status}
