"""PiSAs (HF: ServiceNow/PiSAs; code: github.com/ServiceNow/PiSAs).

265 scenarios (9 tasks) x 4 files: scenario.json, utility.json, appropriateness.json, visibility.json.
Attribute-level appropriateness and visibility are kept SEPARATE and are never collapsed into a scenario label.
Per the GitHub README the paper evaluates the first three tasks (JIRA_allocation, meeting_allocation,
severity_classification: 85 hand-built scenarios); the other six were generated later from task seeds. The
`subset` field records that distinction ("original" vs "expanded").

Status: loader verified, scenario-level only. Attribute-level extraction is not implemented.
The extraction prompt never sees appropriateness, visibility, oracle answers or attribute ids.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterator

from ci_audit.config import load_config
from ci_audit.datasets.base import AdapterError, DatasetAdapter
from ci_audit.schemas import NativeCIValue, NativeReference, SourceRecord
from ci_audit.storage import read_json

FILE_NAMES = ("scenario", "utility", "appropriateness", "visibility")


class PiSAsAdapter(DatasetAdapter):
    name = "pisas"
    source = "pisas"
    subsets = ("original", "expanded")
    default_subsets = ("original", "expanded")
    status = "loader_verified"

    def _original_tasks(self) -> set[str]:
        try:
            cfg = load_config(self.paths.root, "datasets")
            return set(cfg["adapters"]["pisas"]["original_tasks"])
        except Exception:
            return {"JIRA_allocation", "meeting_allocation", "severity_classification"}

    def _scenario_dirs(self):
        base = self.raw / "hf"
        if not base.is_dir():
            raise AdapterError(f"pisas: {base} missing. Run: python -m ci_audit download --dataset pisas")
        for task_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for sc in sorted(p for p in task_dir.iterdir() if p.is_dir() and p.name.startswith("scenario_")):
                yield task_dir.name, sc

    @staticmethod
    def render_scenario(sc: dict) -> str:
        task = sc.get("task", {})
        cast = sc.get("cast", {})
        lines = [
            f"Domain label in release: {sc.get('domain')}",
            f"Task type: {task.get('type')}",
            f"Task description: {sc.get('description') or task.get('description')}",
            f"Executor role: {task.get('executor_role')}",
            "",
            "Cast:",
        ]
        for cid, c in cast.items():
            lines.append(f"- {c.get('name')} ({c.get('role')}, team {c.get('team')}, task slot: {c.get('task_slot')})")
        lines += ["", "Timeline of artifacts:"]
        for t in sc.get("timeline", []):
            vis = t.get("visible_to") or []
            lines.append(f"- [{t.get('timestamp')}] {t.get('type')} by {t.get('author')} (visible to {len(vis)}: {', '.join(vis)}): {t.get('content')}")
        return "\n".join(lines)

    def iter_records(self) -> Iterator[SourceRecord]:
        rev = self.source_revision()
        original = self._original_tasks()
        for task, sc_dir in self._scenario_dirs():
            subset = "original" if task in original else "expanded"
            if subset not in self.selected:
                continue
            docs, raw_text = {}, {}
            for fn in FILE_NAMES:
                p = sc_dir / f"{fn}.json"
                if not p.is_file():
                    raise AdapterError(f"pisas: {p} missing")
                raw_text[fn] = p.read_text(encoding="utf-8")
                docs[fn] = read_json(p)
            sc, util, appr, vis = docs["scenario"], docs["utility"], docs["appropriateness"], docs["visibility"]
            rel = f"{task}/{sc_dir.name}"
            ci: dict[str, NativeCIValue] = {}
            note = "Adapter mapping of PiSAs roles: the executor agent's role is the recipient of information gathered for the task."
            ex_role = sc.get("task", {}).get("executor_role")
            ex = next((c for c in sc.get("cast", {}).values() if c.get("task_slot") == "executor"), None)
            if ex:
                ci["recipient"] = NativeCIValue(value=f"{ex.get('role')} ({ex.get('name')})", source_field="cast[task_slot=executor]", origin="benchmark_mapped", note=note)
            subj = next((c for c in sc.get("cast", {}).values() if c.get("task_slot") == "subject"), None)
            if subj:
                ci["subject"] = NativeCIValue(value=f"{subj.get('role')} ({subj.get('name')})", source_field="cast[task_slot=subject]", origin="benchmark_mapped", note="Cast member whose task_slot is 'subject'.")
            counts = Counter(appr.get("attributes", {}).values())
            util_keep = {k: v for k, v in util.items() if k != "attribute_values"}
            yield SourceRecord(
                record_id=f"{self.name}:{rel}",
                dataset=self.name,
                subset=subset,
                level="scenario",
                source_id=rel,
                source_revision=rev,
                source_files=[f"pisas/hf/{rel}/{fn}.json" for fn in FILE_NAMES],
                content_sha256=self.digest(*(raw_text[fn] for fn in FILE_NAMES)),
                original_text=raw_text["scenario"],
                scenario_text=self.render_scenario(sc),
                text_is_rendered=True,
                native_reference=NativeReference(
                    kind="attribute_appropriateness",
                    label=appr.get("attributes"),
                    description="Per-attribute appropriate/inappropriate labels (appropriateness.json), kept unaggregated. Visibility rules are in source_metadata.visibility.",
                    details={"label_counts": dict(counts), "policy_note": appr.get("policy_note"), "scenario_level_label": None},
                ),
                native_ci=ci,
                native_domain=sc.get("domain"),
                source_metadata={
                    "scenario_id": sc.get("scenario_id"),
                    "task": task,
                    "visibility": vis.get("attributes"),
                    "utility": util_keep,
                },
                links={"task": task, "scenario_dir": sc_dir.name},
            )

    def check(self) -> dict:
        base = super().check()
        recs = list(self.iter_records())
        base["counts"]["by_subset"] = dict(Counter(r.subset for r in recs))
        base["counts"]["tasks"] = len({r.links["task"] for r in recs})
        n_attr = sum(len(r.native_reference.label or {}) for r in recs)
        base["counts"]["annotated_attributes"] = n_attr
        bad = [r.record_id for r in recs if set(r.native_reference.label or {}) != set(r.source_metadata["visibility"] or {})]
        if bad:
            base["problems"].append(f"{len(bad)} scenarios where appropriateness and visibility attribute ids differ (e.g. {bad[0]})")
        return base
