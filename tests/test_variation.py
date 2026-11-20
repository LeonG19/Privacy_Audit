"""Variation pipeline: pools, replacement planning, generation (mock engine), resume, CLI plan-only. No network."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from ci_audit.engines.base import GenerationSettings
from ci_audit.engines.mock import MockEngine
from ci_audit.variation.generate import VariationRun, generate_variations, substitute_in_seed
from ci_audit.variation.knowledge import DomainKnowledge, norm_value
from ci_audit.variation.plan import candidates, plan_variants
from ci_audit.variation.seeds import Seed, assign_domain, load_known_seeds, match_domain, read_subset_file, select_seeds

MD = """# Domain knowledge

## Healthcare

**Scenarios:** 2

### Roles (senders, subjects, recipients)

- **doctor** [sender] — `ds:a`
- **patient** [subject] — `ds:a, ds:b`
- **pharmacist** [recipient] — `ds:b`
- **insurer** [recipient] — `ds:c`

### Attributes (information types)

- **diagnosis** — `ds:a`
- **allergies** — `ds:b`

### Transmission principles (candidate conditions on flow)

- **with the patient's consent** — `ds:a`

**Included scenario IDs:** `ds:a`, `ds:b`

## Finance

### Roles (senders, subjects, recipients)

- **banker** [sender] — `ds:f`
- **client** [subject] — `ds:f`

### Attributes (information types)

- **income** — `ds:f`

### Transmission principles (candidate conditions on flow)

_No non-null values extracted._

**Included scenario IDs:** `ds:f`
"""
CSV = """record_id,dataset,task,model_domain,final_domain,sender_role,subject_role,recipient_role,information_type,attribute,transmission_principle,benchmark_share_label
air:1,airgapagent_r,t,Healthcare,Healthcare,user's AI assistant (setup),user (setup),clinic (setup),pet_ownership,pet ownership; smoker,"visible only to the sender (as extracted: ""visible to 1: x"")",x
"""


@pytest.fixture
def kb(tmp_path):
    md = tmp_path / "domain_knowledge.md"
    md.write_text(MD, encoding="utf-8")
    c = tmp_path / "norm.csv"
    c.write_text(CSV, encoding="utf-8")
    k = DomainKnowledge()
    k.load_domain_knowledge_md(md)
    k.load_norm_elements_csv(c)
    return k


def run(coro):
    return asyncio.run(coro)


def seed(rid="ds:a", domain="Healthcare", level="story", text="Dr. X told the pharmacist about the patient's diagnosis.", **ci):
    base = {"sender": "doctor", "subject": "patient", "recipient": "pharmacist", "information_type": "diagnosis",
            "transmission_principle": "with the patient's consent"}
    base.update(ci)
    return Seed(rid, "ds", text, base, domain, "manual_review", level=level)


# ------------------------------------------------------------------ knowledge
def test_parse_markdown_and_csv(kb):
    roles = {i.value for i in kb.items("Healthcare", "role")}
    assert {"doctor", "patient", "pharmacist", "insurer", "user's AI assistant", "user", "clinic"} <= roles
    attrs = {i.value for i in kb.items("Healthcare", "attribute")}
    assert {"diagnosis", "allergies", "pet ownership", "smoker"} <= attrs          # ';' list split
    tps = {i.value for i in kb.items("Healthcare", "transmission_principle")}
    assert "visible only to the sender" in tps                                      # extraction remark removed
    assert kb.items("Finance", "transmission_principle") == []
    assert kb.record_domain["ds:a"] == "Healthcare" and kb.record_domain["air:1"] == "Healthcare"
    assert next(i for i in kb.items("Healthcare", "role") if i.value == "patient").sources == ["ds:a", "ds:b"]


def test_norm_value_ignores_case_article_setup():
    assert norm_value("The Doctor") == norm_value("doctor (setup)") == norm_value("a doctor.")


# ------------------------------------------------------------------ planning
def test_candidates_exclude_original_other_roles_and_own_record(kb):
    s = seed()
    cands, scope = candidates(s, "sender", kb)
    vals = {c.value for c in cands}
    assert scope == "same_domain"
    assert "doctor" not in vals and "patient" not in vals and "pharmacist" not in vals   # own value + other roles of the flow
    assert "insurer" in vals
    # a value extracted only from this very record is not reused for it
    cands, _ = candidates(seed(rid="ds:c"), "recipient", kb)
    assert "insurer" not in {c.value for c in cands}


def test_cross_domain_fallback_is_flagged_and_strict_skips(kb):
    s = seed(domain="Finance", rid="ds:f", transmission_principle="by letter")
    cands, scope = candidates(s, "transmission_principle", kb)
    assert scope == "cross_domain_fallback" and cands
    plans, skipped = plan_variants([s], kb, 1, ("transmission_principle",), strict_domain=True)
    assert plans == [] and skipped[0]["parameter"] == "transmission_principle"


def test_plan_one_variant_per_existing_param_and_deterministic(kb):
    s = seed(transmission_principle=None)
    a, _ = plan_variants([s], kb, 7)
    b, _ = plan_variants([s], kb, 7)
    assert [p.parameter for p in a] == ["sender", "subject", "recipient", "information_type"]   # null TP not touched
    assert [(p.variant_id, p.new_value) for p in a] == [(p.variant_id, p.new_value) for p in b]
    for p in a:
        changed = [k for k in p.new_ci if p.new_ci[k] != s.ci[k]]
        assert changed == [p.parameter] and p.new_value != p.original_value


def test_per_param_gives_distinct_values(kb):
    plans, _ = plan_variants([seed()], kb, 3, ("recipient",), per_param=2)
    assert len({p.new_value for p in plans}) == len(plans) == 2


def test_max_chars_filters_long_values(kb):
    kb.add("Healthcare", "attribute", "x" * 500, ["ds:z"])
    cands, _ = candidates(seed(), "information_type", kb, max_chars=100)
    assert all(len(c.value) <= 100 for c in cands)


# ------------------------------------------------------------------ seeds
def test_select_per_dataset_is_seeded(kb):
    seeds = [Seed(f"ds:{i}", "ds", "t", {"sender": "a"}, "Healthcare", "x") for i in range(20)]
    one = [s.record_id for s in select_seeds(seeds, None, 5, 1)]
    assert one == [s.record_id for s in select_seeds(seeds, None, 5, 1)] and len(one) == 5
    assert one != [s.record_id for s in select_seeds(seeds, None, 5, 2)]


def test_subset_file_formats(tmp_path, kb):
    known = {"ds:a": seed()}
    (tmp_path / "ids.txt").write_text("# c\nds:a\n")
    assert read_subset_file(tmp_path / "ids.txt", known, kb)[0].record_id == "ds:a"
    (tmp_path / "p.json").write_text(json.dumps({"datasets": {"ds": {"record_ids": ["ds:a"]}}}))
    assert read_subset_file(tmp_path / "p.json", known, kb)[0].record_id == "ds:a"
    row = {"record_id": "mine:1", "dataset": "mine", "scenario_text": "T", "ci_parameters": {"sender": {"value": "doctor"}}, "domain": "Healthcare"}
    (tmp_path / "own.jsonl").write_text(json.dumps(row) + "\n")
    s = read_subset_file(tmp_path / "own.jsonl", known, kb)[0]
    assert s.ci["sender"] == "doctor" and s.domain == "Healthcare"
    (tmp_path / "bad.txt").write_text("nope:1\n")
    with pytest.raises(KeyError):
        read_subset_file(tmp_path / "bad.txt", known, kb)


# ------------------------------------------------------------------ generation
def responder(system, user):
    if "Check a rewritten privacy scenario" in user:
        return json.dumps({"changed_parameter_expressed": True, "old_value_removed": True, "other_parameters_preserved": True,
                           "format_preserved": True, "issues": ""})
    new = re.search(r"- NEW value: (.*)", user).group(1)
    return json.dumps({"new_text": f"REWRITTEN with {new}", "ci_realized": {"sender": None, "subject": None, "recipient": None,
                       "information_type": None, "transmission_principle": None}, "adaptation_notes": "none"})


def make_run(tmp_path, engine, **kw):
    return VariationRun(engine=engine, settings=GenerationSettings(retries=0), out_dir=tmp_path / "out", show_progress=False, **kw)


def test_generate_end_to_end_and_resume(tmp_path, kb):
    plans, _ = plan_variants([seed()], kb, 1)
    eng = MockEngine("m", responder=responder)
    res = run(generate_variations(plans, make_run(tmp_path, eng, verify=True)))
    rows = [json.loads(l) for l in (res["run_dir"] / "variations.jsonl").read_text().splitlines()]
    assert len(rows) == len(plans) == 5 and all(r["status"] == "ok" for r in rows)
    r = rows[0]
    assert r["new_value"] in r["new_text"] and r["verification"]["passed"] and r["original_text"] and r["domain"] == "Healthcare"
    assert (res["run_dir"] / "variations.csv").exists() and (res["run_dir"] / "run_manifest.json").exists()
    calls = eng.calls
    # resume: nothing left to do, no new calls
    res2 = run(generate_variations(plans, make_run(tmp_path, eng, resume=True, verify=True)))
    assert eng.calls == calls and res2["manifest"]["already_done_before_run"] == 5
    # rerun without --resume/--force refuses to mix outputs
    with pytest.raises(FileExistsError):
        run(generate_variations(plans, make_run(tmp_path, eng)))


def test_bad_output_is_retried_then_recorded_as_failure(tmp_path, kb):
    plans, _ = plan_variants([seed()], kb, 1, ("sender",))
    answers = iter(["not json", "still not json"])
    eng = MockEngine("m", responder=lambda s, u: next(answers))
    res = run(generate_variations(plans, make_run(tmp_path, eng, validation_retries=1)))
    assert eng.calls == 2 and res["manifest"]["failed_this_run"] == 1
    fail = json.loads((res["run_dir"] / "failures.jsonl").read_text().splitlines()[0])
    assert fail["stage"] == "validation" and fail["variant_id"].endswith("::sender::0")


def test_unchanged_text_is_rejected(tmp_path, kb):
    s = seed()
    plans, _ = plan_variants([s], kb, 1, ("sender",))
    same = json.dumps({"new_text": s.text, "ci_realized": {k: None for k in s.ci}, "adaptation_notes": ""})
    eng = MockEngine("m", responder=lambda a, b: same)
    res = run(generate_variations(plans, make_run(tmp_path, eng, validation_retries=0)))
    assert res["manifest"]["failed_this_run"] == 1


def test_prompt_contains_original_old_and_new_but_no_instructions_leak(tmp_path, kb):
    plans, _ = plan_variants([seed()], kb, 1, ("recipient",))
    seen = []
    eng = MockEngine("m", responder=lambda s, u: (seen.append(u), responder(s, u))[1])
    run(generate_variations(plans, make_run(tmp_path, eng)))
    u = seen[0]
    assert "Dr. X told the pharmacist" in u and "OLD value: pharmacist" in u and f"NEW value: {plans[0].new_value}" in u
    assert "DOMAIN: Healthcare" in u


def test_structured_seed_is_substituted_without_llm(tmp_path, kb):
    text = "Privacy-sensitive seed (a 5-tuple):\n- sender: doctor\n- subject: patient\n- recipient: pharmacist\n- information type: diagnosis\n- transmission principle: send an email"
    s = seed(level="seed", text=text, transmission_principle="send an email")
    plan = plan_variants([s], kb, 1, ("recipient",))[0][0]
    out = substitute_in_seed(plan)
    assert f"- recipient: {plan.new_value}" in out and "- sender: doctor" in out and "pharmacist" not in out


def test_seed_level_uses_no_llm_calls(tmp_path, kb):
    text = "Seed:\n- sender: doctor\n- subject: patient\n- recipient: pharmacist\n- information type: diagnosis\n- transmission principle: with the patient's consent"
    plans, _ = plan_variants([seed(level="seed", text=text)], kb, 1)
    eng = MockEngine("m", responder=responder)
    res = run(generate_variations(plans, make_run(tmp_path, eng)))
    assert eng.calls == 0 and res["manifest"]["ok_this_run"] == 5


# ------------------------------------------------------------------ real project files
def test_known_seeds_and_plan_on_real_files():
    from conftest import ROOT
    processed = ROOT / "data" / "processed"
    if not (processed / "expanded_pilot" / "results").exists():
        pytest.skip("expanded_pilot results not present")
    kb = DomainKnowledge.load(ROOT, processed)
    known = load_known_seeds(ROOT, processed, kb)
    assert {"confaide_tier2", "confaide_tier3", "privacylens", "mpci_bench"} <= {s.dataset for s in known.values()}
    plans, _ = plan_variants(select_seeds(list(known.values()), None, 3, 1), kb, 1)
    assert plans and all(p.new_value != p.original_value for p in plans)


# ------------------------------------------------------------------ user-provided domain
def test_domain_filter_and_assignment(kb):
    seeds = [seed(rid="ds:1", domain="Healthcare"), seed(rid="ds:2", domain="Finance")]
    assert [s.record_id for s in select_seeds(seeds, None, None, 1, domains=["Finance"])] == ["ds:2"]
    forced = assign_domain(seeds, match_domain("healthcare", kb))      # case-insensitive
    assert {s.domain for s in forced} == {"Healthcare"} and {s.domain_source for s in forced} == {"user"}
    assert seeds[1].domain == "Finance"                                 # originals untouched
    plans, _ = plan_variants(forced, kb, 1, ("information_type",))
    assert all(p.pool_domain == "Healthcare" and p.pool_scope == "same_domain" for p in plans)
    with pytest.raises(ValueError, match="Did you mean 'Healthcare'"):
        match_domain("Helthcare", kb)


def test_per_domain_selection_is_independent_of_dataset_split():
    seeds = []
    for ds in ("a", "b"):
        for dom, n in (("D1", 5), ("D2", 4), ("D3", 3), ("D4", 2), ("D5", 6)):
            seeds += [Seed(f"{ds}:{dom}:{i}", ds, "t", {"sender": "x"}, dom, "x") for i in range(n)]
    both = select_seeds(seeds, None, None, 1, per_domain=3, domains_per_dataset=3)
    assert len(both) == 18
    for ds in ("a", "b"):
        mine = [s for s in both if s.dataset == ds]
        assert len({s.domain for s in mine}) == 3 and "D4" not in {s.domain for s in mine}      # D4 has < 3 samples
        assert all(sum(s.domain == d for s in mine) == 3 for d in {s.domain for s in mine})
        alone = select_seeds(seeds, [ds], None, 1, per_domain=3, domains_per_dataset=3)
        assert [s.record_id for s in alone] == [s.record_id for s in mine]                      # same picks when run separately
