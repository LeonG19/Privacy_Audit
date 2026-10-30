"""AirGapAgent-R (HF: parameterlab/leaky_thoughts; code: github.com/parameterlab/leaky_thoughts).

A *reconstruction* of the unreleased AirGapAgent benchmark. Each record = (synthetic profile, task scenario,
profile field) with a 0/1 label for whether the field should be shared for that task. The release has NO prompt
text (`prompt` is empty) and does not represent sender, recipient or transmission principle - so none are
fabricated. The only CI parameter the data states is the information type (the field).
`approp_matrix.csv` (GitHub) is the task-by-attribute appropriateness table; the adapter cross-checks it against
the per-record labels.

Because there is no scenario prose, `original_text` is a deterministic rendering of the released fields
(`text_is_rendered=True`).
Status: loader verified. LLM extraction prompt not yet validated.
"""
from __future__ import annotations

import csv
import json
from typing import Iterator

from ci_audit.datasets.base import AdapterError, DatasetAdapter
from ci_audit.schemas import NativeCIValue, NativeReference, SourceRecord
from ci_audit.storage import read_json

FILES = {
    "full": "airgapagent-r.json",
    "small": "airgapagent-r-small.json",
    "swap-original": "airgapagent-r-ablation-swap-original.json",
    "swap-flipped": "airgapagent-r-ablation-swap-flipped.json",
}


class AirGapAgentRAdapter(DatasetAdapter):
    name = "airgapagent_r"
    source = "airgapagent_r"
    subsets = tuple(FILES)
    default_subsets = ("full",)
    status = "loader_verified"

    def _matrix(self) -> dict[str, dict[str, int]]:
        rows = list(csv.reader(open(self.require("github", "approp_matrix.csv"), encoding="utf-8")))
        header = rows[0][1:]
        return {r[0]: {t: int(v) for t, v in zip(header, r[1:])} for r in rows[1:] if r}

    def iter_records(self) -> Iterator[SourceRecord]:
        matrix = self._matrix()
        rev = self.source_revision()
        for subset in self.selected:
            data = read_json(self.require("hf", FILES[subset]))
            if not isinstance(data, list):
                raise AdapterError(f"{FILES[subset]}: expected a list")
            profile_ids: dict[str, int] = {}
            for i, r in enumerate(data):
                pj = json.dumps(r["profile"], sort_keys=True, ensure_ascii=False)
                pidx = profile_ids.setdefault(pj, len(profile_ids))
                scen, field = r["scenario"], r["field"]
                col = {f: m.get(scen) for f, m in matrix.items()}
                sid = f"{pidx:02d}:{scen}:{field}"
                text = f"Task domain: {r['domain']}\nTask scenario: {scen}\nProfile attribute considered for sharing: {field}"
                yield SourceRecord(
                    record_id=f"{self.name}:{subset}:{sid}",
                    dataset=self.name,
                    subset=subset,
                    level="attribute",
                    source_id=f"{subset}:{sid}",
                    source_revision=rev,
                    source_files=[f"airgapagent_r/hf/{FILES[subset]}", "airgapagent_r/github/approp_matrix.csv"],
                    content_sha256=self.digest(json.dumps(r, sort_keys=True, ensure_ascii=False)),
                    original_text=text,
                    scenario_text=text,
                    text_is_rendered=True,
                    native_reference=NativeReference(
                        kind="attribute_appropriateness",
                        label=r["label"],
                        scale={"values": {"1": "should share for this task", "0": "should not share for this task"}},
                        description="Per-(task, attribute) ground truth. Not a property of a whole scenario.",
                        details={
                            "approp_matrix_label": matrix.get(field, {}).get(scen),
                            "task_attribute_appropriateness": col,
                        },
                    ),
                    native_ci={"information_type": NativeCIValue(value=field, source_field="field")},
                    native_domain=r["domain"],
                    source_metadata={
                        "profile_index": pidx,
                        "profile_name": r["profile"].get("name"),
                        "ref_answer": r.get("ref_answer"),
                        **({"injected_answer": r["injected_answer"]} if "injected_answer" in r else {}),
                        "prompt_field_empty": not r.get("prompt"),
                    },
                    native_fields={"profile": r["profile"]},
                    links={"file_index": i},
                )

    def check(self) -> dict:
        base = super().check()
        matrix = self._matrix()
        mismatch = n = 0
        for subset in self.selected:
            for r in read_json(self.require("hf", FILES[subset])):
                n += 1
                if matrix.get(r["field"], {}).get(r["scenario"]) != r["label"]:
                    mismatch += 1
        (base["problems"] if mismatch else base["notes"]).append(f"labels vs approp_matrix.csv: {mismatch} mismatches of {n}")
        base["notes"].append("recipient, sender and transmission principle are not represented in this release (left null)")
        return base
