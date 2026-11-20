"""Per-domain pools of CI parameter values.

Sources (all optional, whatever exists is used):
  * <processed>/expanded_pilot/knowledge/domain_knowledge.md
        ConfAIde / PrivacyLens / MPCI-Bench values, grouped by domain: Roles (sender/subject/recipient),
        Attributes (information types), Transmission principles.
  * <root>/knowledge/norm_elements_enrique.csv
        AirGapAgent-R / PiSAs values (one row per record, with `final_domain`).

Mapping used when replacing a parameter:
    sender | subject | recipient  -> "role" pool          (a role can fill any of the three slots)
    information_type              -> "attribute" pool
    transmission_principle        -> "transmission_principle" pool
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

CI_PARAMS = ("sender", "subject", "recipient", "information_type", "transmission_principle")
ROLE_PARAMS = ("sender", "subject", "recipient")
PARAM_KIND = {
    "sender": "role", "subject": "role", "recipient": "role",
    "information_type": "attribute", "transmission_principle": "transmission_principle",
}
KINDS = ("role", "attribute", "transmission_principle")

_LEAD_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)


def norm_value(s: str) -> str:
    """Key for 'is this the same value?' - case, punctuation, leading article and (setup) markers ignored."""
    s = re.sub(r"\(setup\)", "", s, flags=re.I).lower()
    s = re.sub(r"[^\w\s']", " ", s).replace("'", "")
    s = re.sub(r"\s+", " ", s).strip()
    return _LEAD_ARTICLE.sub("", s)


# values that say "nothing was extracted" or hedge about what the source did not state: never usable as a replacement
PLACEHOLDER = re.compile(
    r"^\(?\s*(not (stated|specified|applicable|given)|n/?a|none|null|unknown|unclear|unspecified)\s*\)?\.?$"
    r"|\b(is|are|was|were|being)? ?not (stated|specified|identified|shown|determined)\b|\bunspecified\b|\bunclear\b"
    r"|\bno\b[^;]{0,50}\bstated\b|\bno (explicit|specific|separate) (condition|authorization|authorisation|permission)",
    re.I)


@dataclass
class PoolItem:
    value: str
    kind: str
    domain: str
    sources: list[str] = field(default_factory=list)   # record ids the value was extracted from
    origin: str = ""                                    # which knowledge file it came from


class DomainKnowledge:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, PoolItem]] = {}   # (domain, kind) -> norm -> item
        self.record_domain: dict[str, str] = {}                        # record_id -> final domain (manual review applied)
        self.files: list[str] = []

    # ------------------------------------------------------------------ building
    def add(self, domain: str, kind: str, value: str, sources: Iterable[str] = (), origin: str = "") -> None:
        value = re.sub(r"\s*\(setup\)\s*$", "", value.strip(), flags=re.I).strip()
        if not value or kind not in KINDS or PLACEHOLDER.search(value):
            return
        bucket = self._items.setdefault((domain, kind), {})
        key = norm_value(value)
        if not key:
            return
        item = bucket.get(key)
        if item is None:
            bucket[key] = PoolItem(value, kind, domain, list(dict.fromkeys(sources)), origin)
        else:
            for s in sources:
                if s not in item.sources:
                    item.sources.append(s)

    # ------------------------------------------------------------------ queries
    @property
    def domains(self) -> list[str]:
        return sorted({d for d, _ in self._items})

    def items(self, domain: str, kind: str) -> list[PoolItem]:
        return list(self._items.get((domain, kind), {}).values())

    def all_items(self, kind: str) -> list[PoolItem]:
        return [i for (d, k), b in self._items.items() if k == kind for i in b.values()]

    def sizes(self) -> dict[str, dict[str, int]]:
        return {d: {k: len(self._items.get((d, k), {})) for k in KINDS} for d in self.domains}

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, root: Path, processed: Path, extra_md: Optional[Iterable[Path]] = None,
             extra_csv: Optional[Iterable[Path]] = None) -> "DomainKnowledge":
        kb = cls()
        mds = [processed / "expanded_pilot" / "knowledge" / "domain_knowledge.md", *(extra_md or [])]
        csvs = [root / "knowledge" / "norm_elements_enrique.csv", *(extra_csv or [])]
        for p in mds:
            if Path(p).exists():
                kb.load_domain_knowledge_md(Path(p))
        for p in csvs:
            if Path(p).exists():
                kb.load_norm_elements_csv(Path(p))
        if not kb._items:
            raise FileNotFoundError(
                "no domain knowledge found; expected "
                f"{mds[0]} and/or {csvs[0]} (or pass --knowledge-md / --knowledge-csv)")
        return kb

    _BULLET = re.compile(r"^- \*\*(?P<val>.+?)\*\*(?: \[(?P<tags>[^\]]*)\])?\s+[—-]\s+(?P<ids>.*)$")
    _IDS = re.compile(r"`([^`]+)`")

    def load_domain_knowledge_md(self, path: Path) -> None:
        """Parse the generated domain_knowledge.md (## Domain / ### Roles|Attributes|Transmission principles / bullets)."""
        domain: Optional[str] = None
        kind: Optional[str] = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                domain, kind = line[3:].strip(), None
                continue
            if line.startswith("### "):
                h = line[4:].strip().lower()
                kind = ("role" if h.startswith("roles") else "attribute" if h.startswith("attributes")
                        else "transmission_principle" if h.startswith("transmission") else None)
                continue
            if domain is None:
                continue
            if line.startswith("**Included scenario IDs:**"):
                for rid in self._IDS.findall(line):
                    self.record_domain[rid] = domain
                kind = None
                continue
            if kind is None:
                continue
            m = self._BULLET.match(line)
            if m:
                self.add(domain, kind, m["val"], self._split_ids(m["ids"]), origin=path.name)
        self.files.append(str(path))

    @classmethod
    def _split_ids(cls, text: str) -> list[str]:
        ids = cls._IDS.findall(text)
        out: list[str] = []
        for chunk in ids:
            out.extend(s.strip() for s in chunk.split(", ") if s.strip())
        return out

    def load_norm_elements_csv(self, path: Path) -> None:
        """AirGapAgent-R / PiSAs elements (Enrique): one row per record with final_domain and the five parameters."""
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                domain = (row.get("final_domain") or "").strip()
                rid = (row.get("record_id") or "").strip()
                if not domain:
                    continue
                if rid:
                    self.record_domain.setdefault(rid, domain)
                src = [rid] if rid else []
                for col in ("sender_role", "subject_role", "recipient_role"):
                    self.add(domain, "role", row.get(col) or "", src, origin=path.name)
                # PiSAs rows list several generalised attributes separated by ';' - each one is its own pool value
                for attr in (row.get("attribute") or row.get("information_type") or "").split(";"):
                    self.add(domain, "attribute", attr, src, origin=path.name)
                tp = (row.get("transmission_principle") or "").strip()
                tp = re.sub(r"\s*\(as extracted:.*\)\s*$", "", tp)   # drop the extraction-format remark
                self.add(domain, "transmission_principle", tp, src, origin=path.name)
        self.files.append(str(path))
