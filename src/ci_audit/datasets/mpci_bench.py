"""MPCI-Bench (https://github.com/hpzhang94/MPCI-Bench).

2,052 cases = 1,026 matched pairs (`<id>_pos` appropriate / `<id>_neg` inappropriate). Each case has a seed,
a story, a trace (trajectory) and one VISPR image reference. VISPR images are NOT redistributed and are NOT
downloaded; this adapter is text-only and records every missing image explicitly. Multimodal processing belongs
in a separate adapter (not implemented).

Note for the audit: pos/neg seeds of a pair typically differ in several CI fields at once (recipient AND
principle, often data type/sender/domain too), so they are not minimal single-parameter contrasts.

Status: loader verified against the real release. LLM extraction prompt not yet validated.
"""
from __future__ import annotations

from pathlib import PurePosixPath
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
STORY_MAP = {
    "sender": "data_sender_concrete",
    "subject": "data_subject_concrete",
    "recipient": "data_recipient_concrete",
    "information_type": "data_type_concrete",
}


class MPCIBenchAdapter(DatasetAdapter):
    name = "mpci_bench"
    source = "mpci_bench"
    subsets = ("seed", "story", "trajectory")
    default_subsets = ("seed", "story", "trajectory")
    status = "loader_verified"

    def _data(self) -> list[dict]:
        d = read_json(self.require("github", "dataset", "mpci_bench.json"))
        if not isinstance(d, list):
            raise AdapterError("mpci_bench.json: expected a list")
        return d

    def _image_assets(self, entry: dict) -> dict:
        imgs = []
        for m in entry.get("img_metadata", []):
            vid = PurePosixPath(m["path"]).stem
            local = self.paths.external / "vispr" / "train2017" / f"{vid}.jpg"
            imgs.append(
                {
                    "benchmark_path": m["path"],
                    "vispr_image_id": vid,
                    "vispr_label": m.get("label"),
                    "expected_local_path": str(local),
                    "available": local.is_file(),
                }
            )
        return {
            "image_required": True,
            "images": imgs,
            "all_available": all(i["available"] for i in imgs),
            "processing_mode": "text_only",
            "note": "VISPR images are not redistributed; missing images are recorded, never substituted.",
        }

    def iter_records(self) -> Iterator[SourceRecord]:
        rev = self.source_revision()
        for e in self._data():
            name = e["name"]
            if not (name.endswith("_pos") or name.endswith("_neg")):
                raise AdapterError(f"mpci_bench: case name {name!r} lacks _pos/_neg suffix")
            pair_id, pol = name.rsplit("_", 1)
            other = f"{pair_id}_{'neg' if pol == 'pos' else 'pos'}"
            seed, story, trace = e["seed"], e["story"], e["trace"]
            links = {
                "pair_id": pair_id,
                "polarity": pol,
                "paired_case": other,
                **{f"{lvl}_record_id": f"{self.name}:{name}:{lvl}" for lvl in self.subsets},
            }
            ref = NativeReference(
                kind="pair_polarity",
                label="appropriate" if pol == "pos" else "inappropriate",
                description="README: _pos = contextually appropriate case; _neg = matched inappropriate case. Pair relationship preserved in links.",
                details={"pair_id": pair_id, "paired_case": other},
            )
            assets = self._image_assets(e)
            for lvl in self.selected:
                if lvl == "seed":
                    ci = {k: NativeCIValue(value=str(seed[f]), source_field=f"seed.{f}") for k, f in SEED_MAP.items() if seed.get(f)}
                    orig = text = "Contextual-integrity seed:\n" + "\n".join(f"- {k.replace('_', ' ')}: {seed.get(f)}" for k, f in SEED_MAP.items()) + f"\n- transmission method: {seed.get('transmission_method')}"
                    rendered = True
                elif lvl == "story":
                    ci = {k: NativeCIValue(value=str(story[f]), source_field=f"story.{f}") for k, f in STORY_MAP.items() if story.get(f)}
                    if seed.get("transmission_principle"):
                        ci["transmission_principle"] = NativeCIValue(value=seed["transmission_principle"], source_field="seed.transmission_principle")
                    orig = text = story.get("content", "")
                    rendered = False
                else:
                    ci = {k: NativeCIValue(value=str(seed[f]), source_field=f"seed.{f}") for k, f in SEED_MAP.items() if seed.get(f)}
                    orig = text = f"User instruction: {trace.get('user_instruction', '')}\n\nAgent trajectory:\n{trace.get('executable_trajectory', '')}"
                    rendered = True
                yield SourceRecord(
                    record_id=f"{self.name}:{name}:{lvl}",
                    dataset=self.name,
                    subset=lvl,
                    level=lvl,
                    source_id=f"{name}:{lvl}",
                    source_revision=rev,
                    source_files=["mpci_bench/github/dataset/mpci_bench.json", "mpci_bench/github/dataset/required_image_ids.json"],
                    content_sha256=self.digest(name, lvl, orig),
                    original_text=orig,
                    scenario_text=text,
                    text_is_rendered=rendered,
                    native_reference=ref,
                    native_ci=ci,
                    native_domain=seed.get("contextual_domain"),
                    source_metadata={
                        "transmission_method": seed.get("transmission_method"),
                        "data_sender_name": seed.get("data_sender_name"),
                        "img_metadata": e.get("img_metadata"),
                        **({"final_action": trace.get("final_action"), "toolkits": trace.get("toolkits")} if lvl == "trajectory" else {}),
                    },
                    links=links,
                    assets=assets,
                )

    def check(self) -> dict:
        base = super().check()
        data = self._data()
        names = [e["name"] for e in data]
        pairs: dict[str, set] = {}
        for n in names:
            pairs.setdefault(n.rsplit("_", 1)[0], set()).add(n.rsplit("_", 1)[1])
        broken = [p for p, s in pairs.items() if s != {"pos", "neg"}]
        if broken:
            base["problems"].append(f"{len(broken)} pairs lack a pos/neg partner")
        base["counts"].update(cases=len(data), pairs=len(pairs))
        req = set(read_json(self.require("github", "dataset", "required_image_ids.json")))
        used = {PurePosixPath(m["path"]).stem for e in data for m in e.get("img_metadata", [])}
        if req != used:
            base["problems"].append(f"required_image_ids.json ({len(req)}) != image ids used in cases ({len(used)})")
        avail = sum(1 for i in used if (self.paths.external / "vispr" / "train2017" / f"{i}.jpg").is_file())
        base["counts"].update(required_images=len(used), images_available_locally=avail)
        base["notes"].append(f"MISSING image assets: {len(used) - avail}/{len(used)} (VISPR not redistributed; text-only processing)")
        return base
