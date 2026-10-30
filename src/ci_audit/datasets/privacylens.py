"""PrivacyLens (https://github.com/SALT-NLP/PrivacyLens, HF: SALT-NLP/PrivacyLens).

Each of the 493 data points holds a seed (abstract 5-tuple), a vignette (story + concrete CI fields) and a
trajectory (agent actions). The adapter yields three linked records per data point (levels seed/vignette/trajectory).

Native reference: every seed is a *negative* privacy norm by construction (README: "A negative privacy norm
expressed as a 5-tuple"), so the reference is kind="violation_by_construction" - NOT a balanced
appropriate/inappropriate label. Vignette and trajectory inherit it from their seed.

Status: loader verified against the real release. LLM extraction prompt not yet validated.
"""
from __future__ import annotations

import json
from typing import Iterator

from ci_audit.datasets.base import AdapterError, DatasetAdapter
from ci_audit.schemas import NativeCIValue, NativeReference, SourceRecord
from ci_audit.storage import read_json

SEED_MAP = {
    "sender": "data_sender",
    "subject": "data_subject",
    "recipient": "data_recipient",
    "information_type": "data_type",
    "transmission_principle": "transmission_principle",
}
VIGNETTE_MAP = {
    "sender": "data_sender_concrete",
    "subject": "data_subject_concrete",
    "recipient": "data_recipient_concrete",
    "information_type": "data_type_concrete",
}


class PrivacyLensAdapter(DatasetAdapter):
    name = "privacylens"
    source = "privacylens"
    subsets = ("seed", "vignette", "trajectory")
    default_subsets = ("seed", "vignette", "trajectory")
    status = "loader_verified"

    def _data(self) -> list[dict]:
        d = read_json(self.require("hf", "main_data.json"))
        if not isinstance(d, list):
            raise AdapterError("privacylens main_data.json: expected a list")
        return d

    def iter_records(self) -> Iterator[SourceRecord]:
        rev = self.source_revision()
        for dp in self._data():
            cid, seed, vig, traj = dp["name"], dp["seed"], dp["vignette"], dp["trajectory"]
            ids = {lvl: f"{self.name}:{cid}:{lvl}" for lvl in ("seed", "vignette", "trajectory")}
            links = {"case_id": cid, **{f"{k}_record_id": v for k, v in ids.items()}}
            meta_seed = {
                "seed_source": seed.get("source"),
                "source_details": seed.get("source_details"),
                "data_sender_name": seed.get("data_sender_name"),
            }
            ref = NativeReference(
                kind="violation_by_construction",
                label="negative_privacy_norm",
                description="PrivacyLens seeds are privacy-sensitive *negative* norms; the flow described is the inappropriate one.",
                details={"inherited_from": "seed", "seed_source": seed.get("source")},
            )

            def ci_from(mapping, src, prefix, fallback=None):
                out = {}
                for k, f in mapping.items():
                    if src.get(f):
                        out[k] = NativeCIValue(value=str(src[f]), source_field=f"{prefix}.{f}")
                if fallback is not None and "transmission_principle" not in out and fallback.get("transmission_principle"):
                    out["transmission_principle"] = NativeCIValue(value=fallback["transmission_principle"], source_field="seed.transmission_principle")
                return out

            for lvl in self.selected:
                warns = []
                if lvl == "seed":
                    orig = json.dumps({k: seed.get(k) for k in SEED_MAP.values()} | {"data_sender_name": seed.get("data_sender_name")}, ensure_ascii=False, indent=2)
                    text = (
                        "Privacy-sensitive seed (a 5-tuple):\n"
                        + "\n".join(f"- {k.replace('_', ' ')}: {seed.get(f)}" for k, f in SEED_MAP.items())
                    )
                    ci = ci_from(SEED_MAP, seed, "seed")
                    rendered = True
                    sid_hash = self.digest(json.dumps(seed, sort_keys=True, ensure_ascii=False))
                elif lvl == "vignette":
                    orig = text = vig.get("story", "")
                    ci = ci_from(VIGNETTE_MAP, vig, "vignette", fallback=seed)
                    rendered = False
                    sid_hash = self.digest(json.dumps(vig, sort_keys=True, ensure_ascii=False))
                else:
                    orig = text = f"User instruction: {traj.get('user_instruction', '')}\n\nAgent trajectory:\n{traj.get('executable_trajectory', '')}"
                    ci = ci_from(SEED_MAP, seed, "seed")  # trajectory was simulated from the seed
                    rendered = True
                    sid_hash = self.digest(json.dumps(traj, sort_keys=True, ensure_ascii=False))
                meta = dict(meta_seed)
                if lvl == "trajectory":
                    meta |= {"toolkits": traj.get("toolkits"), "final_action": traj.get("final_action"), "user_name": traj.get("user_name"), "sensitive_info_items": traj.get("sensitive_info_items")}
                yield SourceRecord(
                    record_id=ids[lvl],
                    dataset=self.name,
                    subset=lvl,
                    level=lvl,
                    source_id=f"{cid}:{lvl}",
                    source_revision=rev,
                    source_files=["privacylens/hf/main_data.json"],
                    content_sha256=sid_hash,
                    original_text=orig,
                    scenario_text=text,
                    text_is_rendered=rendered,
                    native_reference=ref,
                    native_ci=ci,
                    source_metadata=meta,
                    native_fields={"seed": seed, "vignette": vig, "trajectory": traj} if lvl == "seed" else {},
                    links=links,
                    parse_warnings=warns,
                )

    def check(self) -> dict:
        base = super().check()
        data = self._data()
        base["counts"]["data_points"] = len(data)
        gh = self.raw / "github" / "data" / "main_data.json"
        if gh.exists():
            same = read_json(gh) == data
            (base["notes"] if same else base["problems"]).append(
                "HF main_data.json is identical to GitHub data/main_data.json" if same else "HF and GitHub main_data.json DIFFER"
            )
        for d in data:
            for lvl in ("seed", "vignette", "trajectory"):
                if lvl not in d:
                    base["problems"].append(f"{d.get('name')}: missing {lvl}")
        return base
