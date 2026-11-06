"""Pilot-evaluation tooling: sampling, scoring, GPU summary, per-record spend accounting, blind mode, --ids-file.

All text is invented ('[TEST FIXTURE]'); nothing needs data/raw or the network.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import pytest

from ci_audit.cli import read_ids_file
from ci_audit.engines.base import Engine, EngineError, GenerationSettings, ModelResponse
from ci_audit.engines.mock import MockEngine, empty_extraction
from ci_audit.evaluation import gpu, sample as sample_mod
from ci_audit.evaluation import scoring
from ci_audit.evaluation.sample import MISSING, build_sample, quotas, select_dataset, write_sample
from ci_audit.pipeline import PipelineError, RunSpec, run
from ci_audit.processing.ci_extraction import ExtractionFailure, _Fail, call_and_parse, extract_record
from ci_audit.processing.validation import merge_ci
from ci_audit.prompts import BLIND_SUFFIX, load_taxonomy
from ci_audit.schemas import CI_PARAMS, LLMExtraction, NativeCIValue, NativeReference, SourceRecord
from conftest import ROOT, valid_extraction_json


# =========================================================================== sample.py
def _rec(rid: str, subset: str, **links) -> SourceRecord:
    return SourceRecord(
        record_id=rid, dataset="fake_ds", subset=subset, level="scenario", source_id=rid, source_revision="rev", source_files=["f"],
        content_sha256=f"sha-{rid}", original_text=f"[TEST FIXTURE] {rid}", scenario_text=f"[TEST FIXTURE] {rid}",
        native_reference=NativeReference(kind="none"), links=links,
    )


class FakeAdapter:
    """Stand-in adapter; `RECORDS` is set per test through `fake_adapter`."""

    RECORDS: list[SourceRecord] = []

    def __init__(self, paths, subsets=None):
        self.selected = tuple(subsets or ("a", "b"))

    def iter_records(self):
        return iter(self.RECORDS)

    def source_revision(self):
        return "rev-1"


@pytest.fixture
def fake_adapter(monkeypatch):
    def install(records: list[SourceRecord]) -> None:
        cls = type("FA", (FakeAdapter,), {"RECORDS": records})
        monkeypatch.setattr(sample_mod, "get_adapter_class", lambda name: cls)

    return install


def test_quotas_split_evenly_and_give_remainder_to_first_subsets():
    assert quotas(20, ["a", "b"]) == {"a": 10, "b": 10}
    assert quotas(5, ["a", "b", "c"]) == {"a": 2, "b": 2, "c": 1}
    assert quotas(0, ["a", "b"]) == {"a": 0, "b": 0}
    assert sum(quotas(7, ["a", "b", "c", "d"]).values()) == 7


def test_select_dataset_is_seeded_and_stratified(paths, fake_adapter):
    fake_adapter([_rec(f"{s}-{i}", s) for s in "ab" for i in range(10)])
    r1, info1 = select_dataset(paths, "fake_ds", ["a", "b"], 6, seed=1)
    r2, info2 = select_dataset(paths, "fake_ds", ["a", "b"], 6, seed=1)
    r3, _ = select_dataset(paths, "fake_ds", ["a", "b"], 6, seed=2)
    assert [r.record_id for r in r1] == [r.record_id for r in r2]  # same seed, same draw
    assert [r.record_id for r in r1] != [r.record_id for r in r3]  # different seed, different draw
    assert Counter(r.subset for r in r1) == {"a": 3, "b": 3}
    assert info1["population"] == {"a": 10, "b": 10}
    assert info1["record_ids"] == [r.record_id for r in r1] == info2["record_ids"]
    assert info1["content_sha256"] == {r.record_id: f"sha-{r.record_id}" for r in r1}
    assert info1["source_revision"] == "rev-1"


def test_select_dataset_draw_does_not_depend_on_adapter_order(paths, fake_adapter):
    recs = [_rec(f"a-{i}", "a") for i in range(10)]
    fake_adapter(recs)
    fwd, _ = select_dataset(paths, "fake_ds", ["a"], 4, seed=7)
    fake_adapter(list(reversed(recs)))
    rev, _ = select_dataset(paths, "fake_ds", ["a"], 4, seed=7)
    assert [r.record_id for r in fwd] == [r.record_id for r in rev]


# Records sharing a group INSIDE one subset (e.g. both sides of an MPCI pair at the seed level) must not both be drawn.


@pytest.mark.parametrize("link", ["case_id", "pair_id", "label_index"])
def test_select_dataset_takes_at_most_one_record_per_group(paths, fake_adapter, link):
    # 6 records in 3 groups (two each); asking for 3 must hit every group exactly once
    fake_adapter([_rec(f"a-{i}", "a", **{link: i % 3}) for i in range(6)])
    picked, _ = select_dataset(paths, "fake_ds", ["a"], 3, seed=0)
    assert sorted(r.links[link] for r in picked) == [0, 1, 2]


def test_select_dataset_groups_span_subsets(paths, fake_adapter):
    # the same scenario in subsets a and b (shared label_index) must not be drawn twice
    fake_adapter([_rec(f"{s}-{i}", s, label_index=i) for s in "ab" for i in range(4)])
    picked, _ = select_dataset(paths, "fake_ds", ["a", "b"], 4, seed=3)
    assert sorted(r.links["label_index"] for r in picked) == [0, 1, 2, 3]


def test_select_dataset_records_without_links_are_their_own_group(paths, fake_adapter):
    fake_adapter([_rec(f"a-{i}", "a") for i in range(5)])
    picked, _ = select_dataset(paths, "fake_ds", ["a"], 5, seed=0)
    assert len({r.record_id for r in picked}) == 5


def test_select_dataset_raises_when_too_few_distinct_groups(paths, fake_adapter):
    fake_adapter([_rec(f"a-{i}", "a", case_id="same") for i in range(5)])
    with pytest.raises(ValueError, match="only 1 distinct records available, 2 requested"):
        select_dataset(paths, "fake_ds", ["a"], 2, seed=0)


def test_select_dataset_raises_when_subset_has_no_records(paths, fake_adapter):
    fake_adapter([_rec("a-0", "a")])
    with pytest.raises(ValueError, match="b: only 0"):
        select_dataset(paths, "fake_ds", ["a", "b"], 2, seed=0)


def test_select_dataset_on_confaide_fixture_never_pairs_2a_with_2b_of_same_scenario(confaide_paths):
    picked, info = select_dataset(confaide_paths, "confaide_tier2", ["2a", "2b"], 2, seed=0)
    assert Counter(r.subset for r in picked) == {"2a": 1, "2b": 1}
    assert len({r.links["label_index"] for r in picked}) == 2
    assert info["population"] == {"2a": 3, "2b": 3}


def _cfg(n=2, seed=0):
    return {"sample_version": 1, "seed": seed, "n_per_dataset": n, "datasets": {"confaide_tier2": {"subsets": ["2a", "2b"]}}}


def test_build_sample_structure(confaide_paths):
    out, records = build_sample(confaide_paths, _cfg())
    assert out["sample_version"] == 1 and out["seed"] == 0 and out["n_per_dataset"] == 2 and out["created_at"]
    assert out["datasets"]["confaide_tier2"]["record_ids"] == [r.record_id for r in records]
    assert len(records) == 2


def test_write_sample_writes_three_files_and_reference_sheet_is_blank_for_humans(confaide_paths, tmp_path):
    out_dir = tmp_path / "pilot"
    files = write_sample(confaide_paths, _cfg(), out_dir)
    assert set(files) == {"sample", "reference", "scenarios"} and all(p.exists() for p in files.values())
    sample = json.loads(files["sample"].read_text())
    with open(files["reference"], encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["record_id"] for r in rows] == sample["datasets"]["confaide_tier2"]["record_ids"]
    for r in rows:
        assert all(r[f"gold_{p}"] == "" for p in CI_PARAMS) and r["gold_domain"] == "" and r["annotator"] == ""
    assert "[TEST FIXTURE]" in files["scenarios"].read_text()


@pytest.mark.parametrize("victim", ["pilot_sample.json", "reference_annotations.csv"])
def test_write_sample_refuses_to_overwrite_shared_files(confaide_paths, tmp_path, victim):
    out_dir = tmp_path / "pilot"
    out_dir.mkdir()
    (out_dir / victim).write_text("ANNOTATED BY HAND")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_sample(confaide_paths, _cfg(), out_dir)
    assert (out_dir / victim).read_text() == "ANNOTATED BY HAND"
    assert not (out_dir / "pilot_scenarios.md").exists()  # nothing partially written


def test_write_sample_may_overwrite_scenarios_markdown_only(confaide_paths, tmp_path):
    out_dir = tmp_path / "pilot"
    out_dir.mkdir()
    (out_dir / "pilot_scenarios.md").write_text("old")
    files = write_sample(confaide_paths, _cfg(), out_dir)
    assert files["scenarios"].read_text() != "old"


# =========================================================================== scoring: matching
def test_norm_lowercases_strips_punctuation_articles_and_unicode_forms():
    assert norm_("The Quick, Brown fox!") == "quick brown fox"
    assert norm_("ＡＢＣ") == "abc"  # NFKC full-width letters
    assert norm_("  A   an the ") == ""
    assert norm_("Pat’s gym") == norm_("Pat's gym")


def norm_(s: str) -> str:
    return scoring.norm(s)


def test_token_f1_identical_disjoint_partial_and_empty():
    assert scoring.token_f1("your gym", "Your Gym.") == 1.0
    assert scoring.token_f1("alpha", "beta") == 0.0
    assert scoring.token_f1("", "beta") == 0.0
    assert scoring.token_f1("", "") == 0.0
    # tokens {red, fox} vs {red, fox, jumps, high}: p=1, r=.5 -> 2/3
    assert scoring.token_f1("red fox", "red fox jumps high") == pytest.approx(2 / 3)


def test_token_f1_is_symmetric():
    a, b = "the employer's HR team", "HR team of the employer"
    assert scoring.token_f1(a, b) == pytest.approx(scoring.token_f1(b, a))


def test_parse_gold_variants():
    assert scoring.parse_gold(None) is None
    assert scoring.parse_gold("") is None
    assert scoring.parse_gold("   ") is None
    assert scoring.parse_gold("<missing>") == MISSING
    assert scoring.parse_gold("  <MISSING> ") == MISSING
    assert scoring.parse_gold("the gym") == ["the gym"]
    assert scoring.parse_gold("a gym | the bank ||  ") == ["a gym", "the bank"]


def test_parse_gold_with_only_separators_is_an_annotated_cell_with_no_values():
    # documents current behaviour: "|" is neither "not annotated" nor "<missing>"
    assert scoring.parse_gold(" | ") is None  # only separators = not annotated


def test_wilson_bounds_and_degenerate_inputs():
    assert scoring.wilson(0, 0) is None
    lo, hi = scoring.wilson(10, 10)
    assert hi == 1.0 and 0.7 < lo < 0.75  # 10/10 is not 100% certain
    lo, hi = scoring.wilson(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.3
    lo, hi = scoring.wilson(5, 10)
    assert lo < 0.5 < hi and lo == pytest.approx(1 - hi, abs=1e-3)
    assert scoring.wilson(50, 100)[1] - scoring.wilson(50, 100)[0] < scoring.wilson(5, 10)[1] - scoring.wilson(5, 10)[0]


# =========================================================================== scoring: outcomes
def _rec_ci(**params):
    """Minimal extraction record: params = {name: (value, origin)}."""
    cp = {p: {"value": None, "origin": "none"} for p in CI_PARAMS}
    for k, (v, o) in params.items():
        cp[k] = {"value": v, "origin": o}
    return {"ci_parameters": cp}


def _outcome(gold_cell, rec, judgments=None, pending=None, auto_f1=0.8, param="recipient"):
    pending = {} if pending is None else pending
    out = scoring.ci_outcome("r1", param, scoring.parse_gold(gold_cell), rec, judgments or {}, auto_f1, pending, "sys:m", "ds", gold_cell)
    return out, pending


def test_ci_outcome_unannotated_and_failed():
    assert _outcome("", _rec_ci())[0] == "unannotated"
    assert _outcome("", None)[0] == "unannotated"  # unannotated wins over failed
    assert _outcome("the gym", None)[0] == "failed"


def test_ci_outcome_benchmark_origin_is_excluded_even_when_value_matches():
    for origin in ("benchmark", "benchmark_mapped"):
        assert _outcome("the gym", _rec_ci(recipient=("the gym", origin)))[0] == "benchmark_value"


def test_ci_outcome_missing_reference():
    assert _outcome(MISSING, _rec_ci())[0] == "correct_null"
    assert _outcome(MISSING, _rec_ci(recipient=("the gym", "llm")))[0] == "invented"


def test_ci_outcome_missed_when_reference_states_value_but_model_gave_null():
    assert _outcome("the gym", _rec_ci())[0] == "missed"


def test_ci_outcome_correct_by_normalised_exact_match_or_any_alternative():
    assert _outcome("the gym", _rec_ci(recipient=("A Gym.", "llm")))[0] == "correct"
    assert _outcome("the gym | the bank", _rec_ci(recipient=("bank", "llm")))[0] == "correct"


def test_ci_outcome_auto_accepts_at_f1_threshold_and_not_below():
    # gold {red, fox, jumps, high}, model {red, fox, jumps}: p=1 r=.75 -> f1 ~= .857
    rec = _rec_ci(recipient=("red fox jumps", "llm"))
    assert _outcome("red fox jumps high", rec, auto_f1=0.85)[0] == "correct"
    out, pending = _outcome("red fox jumps high", rec, auto_f1=0.9)
    assert out == "pending" and len(pending) == 1


def test_ci_outcome_pending_queues_pair_with_best_f1_and_accumulates_models():
    pending: dict = {}
    rec = _rec_ci(recipient=("red fox", "llm"))
    _outcome("red fox jumps high | blue cat", rec, pending=pending)
    scoring.ci_outcome("r1", "recipient", scoring.parse_gold("red fox jumps high | blue cat"), rec, {}, 0.8, pending, "other:m", "ds",
                       "red fox jumps high | blue cat")
    (row,) = pending.values()
    assert row["verdict"] == "" and row["model_value"] == "red fox" and row["reference"] == "red fox jumps high | blue cat"
    assert row["token_f1"] == round(2 / 3, 2) and row["models"] == {"sys:m", "other:m"}


@pytest.mark.parametrize("verdict", ["correct", "partial", "incorrect"])
def test_ci_outcome_uses_human_verdict_case_insensitively(verdict):
    rec = _rec_ci(recipient=("some org", "llm"))
    key = scoring._jkey("r1", "recipient", "some org", "the gym")
    j = {key: {"verdict": f" {verdict.upper()} "}}
    out, pending = _outcome("the gym", rec, judgments=j)
    assert out == verdict and not pending


def test_ci_outcome_blank_or_invalid_verdict_stays_pending():
    rec = _rec_ci(recipient=("some org", "llm"))
    key = scoring._jkey("r1", "recipient", "some org", "the gym")
    for v in ("", "maybe"):
        out, pending = _outcome("the gym", rec, judgments={key: {"verdict": v}})
        assert out == "pending" and len(pending) == 1


def test_ci_outcome_verdict_lookup_is_normalisation_insensitive():
    rec = _rec_ci(recipient=("The Some-Org!", "llm"))
    key = scoring._jkey("r1", "recipient", "some org", "the gym")
    assert _outcome("the gym", rec, judgments={key: {"verdict": "partial"}})[0] == "partial"


def test_ci_outcome_gold_with_only_separators_does_not_crash():
    # a stray "|" in a reference cell counts as not annotated instead of aborting the scoring run
    assert _outcome(" | ", _rec_ci(recipient=("the gym", "llm")))[0] == "unannotated"


# ----------------------------------------------------------------- domain
@pytest.mark.parametrize("ref,rec,expected", [
    ({"gold_domain": ""}, {"domain": {"domain": "Health"}}, "unannotated"),
    ({}, None, "unannotated"),
    ({"gold_domain": "Health"}, None, "failed"),
    ({"gold_domain": "Health"}, {"domain": {"domain": " health "}}, "correct"),
    ({"gold_domain": "Health", "gold_domain_alt": "Finance | Education"}, {"domain": {"domain": "Education"}}, "acceptable_alt"),
    ({"gold_domain": "Health", "gold_domain_alt": "Finance"}, {"domain": {"domain": "Retail"}}, "incorrect"),
    ({"gold_domain": "Health", "gold_domain_alt": None}, {"domain": {"domain": "Retail"}}, "incorrect"),
    ({"gold_domain": "Health", "gold_domain_alt": " | "}, {"domain": {"domain": ""}}, "incorrect"),
])
def test_domain_outcome(ref, rec, expected):
    assert scoring.domain_outcome(ref, rec) == expected


# ----------------------------------------------------------------- param_metrics
def test_param_metrics_rates():
    c = Counter(correct=3, correct_null=1, partial=1, incorrect=1, pending=2, missed=1, invented=1, failed=1)
    m = scoring.param_metrics(c)
    assert m["n"] == 11 and m["pending"] == 2
    decided = 11 - 2
    assert m["accuracy_strict"] == round(4 / decided, 3)
    assert m["accuracy_lenient"] == round(5 / decided, 3)
    assert m["invention_rate"] == 0.5  # 1 invented / (1 correct_null + 1 invented)
    assert m["miss_rate"] == round(1 / 8, 3)  # stated = 3 + 1 + 1 + 2 + 1
    assert m["accuracy_strict_ci95"] == scoring.wilson(4, decided)


def test_param_metrics_excludes_benchmark_and_unannotated_and_handles_empty():
    m = scoring.param_metrics(Counter(benchmark_value=5, unannotated=5))
    assert m["n"] == 0 and m["accuracy_strict"] is None and m["accuracy_strict_ci95"] is None
    assert m["invention_rate"] is None and m["miss_rate"] is None


def test_param_metrics_all_pending_has_no_accuracy_yet():
    m = scoring.param_metrics(Counter(pending=4))
    assert m["n"] == 4 and m["accuracy_strict"] is None and m["accuracy_lenient"] is None


# ----------------------------------------------------------------- merge_judgments
def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _pending_row(rid="r1", value="some org", ref="the gym", models=("sys:m",)):
    key = scoring._jkey(rid, "recipient", value, ref)
    return key, {"record_id": rid, "dataset": "ds", "param": "recipient", "model_value": value, "reference": ref,
                 "verdict": "", "token_f1": 0.0, "models": set(models)}


def _existing_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=scoring.JUDGMENT_FIELDS)
        w.writeheader()
        w.writerows(rows)


def test_merge_judgments_creates_file_with_pending_rows(tmp_path):
    path = tmp_path / "judgments.csv"
    k, row = _pending_row()
    scoring.merge_judgments(path, {k: row})
    (r,) = _read(path)
    assert r["verdict"] == "" and r["models"] == "sys:m" and r["model_value"] == "some org"


def test_merge_judgments_without_pending_and_without_file_writes_nothing(tmp_path):
    path = tmp_path / "judgments.csv"
    scoring.merge_judgments(path, {})
    assert not path.exists()


def test_merge_judgments_keeps_human_verdicts_and_notes_untouched_and_appends_new(tmp_path):
    path = tmp_path / "judgments.csv"
    human = {f: "" for f in scoring.JUDGMENT_FIELDS} | {
        "record_id": "r0", "dataset": "ds", "param": "recipient", "model_value": "a bank", "reference": "the gym",
        "verdict": "partial", "token_f1": "0.4", "models": "sys:m", "judged_by": "alice", "note": "close enough"}
    _existing_csv(path, [human])
    k_new, new = _pending_row(rid="r1")
    k_old, same_as_human = _pending_row(rid="r0", value="a bank", ref="the gym", models=("sys:m",))
    scoring.merge_judgments(path, {k_new: new, k_old: same_as_human})
    rows = _read(path)
    assert rows[0] == human  # byte-for-byte what the human left
    assert len(rows) == 2 and rows[1]["record_id"] == "r1" and rows[1]["verdict"] == ""


def test_merge_judgments_adds_new_model_to_existing_row_without_changing_verdict(tmp_path):
    path = tmp_path / "judgments.csv"
    k, row = _pending_row(models=("zeta:m",))
    human = {f: "" for f in scoring.JUDGMENT_FIELDS} | {
        "record_id": "r1", "dataset": "ds", "param": "recipient", "model_value": "SOME ORG.", "reference": "the gym",
        "verdict": "incorrect", "models": "alpha:m", "judged_by": "bob"}
    _existing_csv(path, [human])
    scoring.merge_judgments(path, {k: row})  # same pair up to normalisation
    (r,) = _read(path)
    assert r["verdict"] == "incorrect" and r["judged_by"] == "bob" and r["model_value"] == "SOME ORG."
    assert r["models"] == "alpha:m, zeta:m"


def test_merge_judgments_is_idempotent(tmp_path):
    path = tmp_path / "judgments.csv"
    k, row = _pending_row()
    scoring.merge_judgments(path, {k: row})
    first = path.read_text()
    scoring.merge_judgments(path, {k: _pending_row()[1]})
    assert path.read_text() == first


# ----------------------------------------------------------------- score end to end
def _full_rec(rid: str, params: dict, domain: str = "Health", origin_default="llm", warnings=None, usage=None, latency=1.0,
              native_ci=None, attempts=1):
    cp = {p: {"value": None, "origin": "none"} for p in CI_PARAMS}
    for k, v in params.items():
        cp[k] = {"value": v, "origin": origin_default}
    return {
        "record_id": rid, "ci_parameters": cp, "domain": {"domain": domain}, "native_ci": native_ci or {},
        "validation_warnings": warnings or [],
        "extraction_metadata": {"attempts": attempts, "status": "ok" if not warnings else "ok_with_warnings", "structured_output_mode": "none",
                                "usage": usage or {"prompt_tokens": 100, "completion_tokens": 10}, "latency_s": latency},
    }


def _run_dict(ok: dict, fail=None, privacy=None, blind=True, dataset="ds", model="m", engine="eng", started="2026-01-01T00:00:00+00:00",
              finished="2026-01-01T00:01:40+00:00"):
    return {"dir": f"/runs/{engine}__{model}", "dataset": dataset, "engine": engine, "model": model, "blind": blind, "ok": ok, "fail": fail or {},
            "privacy": privacy or {}, "manifest": {"started_at": started, "finished_at": finished, "concurrency": 2}}


def _refs(rows: list[dict]) -> list[dict]:
    base = {f"gold_{p}": "" for p in CI_PARAMS} | {"gold_domain": "", "gold_domain_alt": "", "gold_privacy": "", "subset": "all"}
    return [base | r for r in rows]


def test_score_end_to_end_counts_every_outcome(tmp_path):
    ids = ["r1", "r2", "r3"]
    sample = {"datasets": {"ds": {"record_ids": ids, "population": {"all": 30}}}}
    refs = _refs([
        {"record_id": "r1", "gold_recipient": "the gym", "gold_sender": "<missing>", "gold_subject": "Pat | the member",
         "gold_domain": "Health", "gold_domain_alt": "Fitness"},
        {"record_id": "r2", "gold_recipient": "the bank", "gold_sender": "<missing>", "gold_domain": "Finance"},
        {"record_id": "r3", "gold_recipient": "the employer", "gold_domain": "Work"},  # r3 fails
    ])
    ok = {
        "r1": _full_rec("r1", {"recipient": "A gym", "sender": None, "subject": "the member"}, domain="fitness"),
        "r2": _full_rec("r2", {"recipient": "some unrelated org", "sender": "Sam"}, domain="Retail"),
    }
    fail = {"r3": {"record_id": "r3", "error_type": "InvalidModelOutput", "usage": {"prompt_tokens": 50, "completion_tokens": 5}, "latency_s": 2.0}}
    jpath = tmp_path / "judgments.csv"
    res = scoring.score(sample, refs, [_run_dict(ok, fail)], jpath, {})
    d = res["systems"]["eng:m"]["datasets"]["ds"]
    rec = d["ci"]["recipient"]["outcomes"]
    assert rec == {"correct": 1, "pending": 1, "failed": 1}
    assert d["ci"]["sender"]["outcomes"] == {"correct_null": 1, "invented": 1, "unannotated": 1}
    assert d["ci"]["subject"]["outcomes"] == {"correct": 1, "unannotated": 2}
    assert d["ci"]["information_type"]["outcomes"] == {"unannotated": 3}
    assert d["domain"]["outcomes"] == {"acceptable_alt": 1, "incorrect": 1, "failed": 1}
    assert d["domain"]["accuracy_strict"] == 0.0 and d["domain"]["accuracy_with_alternatives"] == round(1 / 3, 3)
    assert res["pending_judgments"] == 1
    assert d["validity"]["sampled"] == 3 and d["validity"]["valid"] == 2 and d["validity"]["failed_by_type"] == {"InvalidModelOutput": 1}
    assert res["annotation_coverage"] == {"ds": 3}
    # the unresolved pair was appended for a human
    (row,) = _read(jpath)
    assert row["model_value"] == "some unrelated org" and row["verdict"] == "" and row["models"] == "eng:m"
    # overall + 'all' roll-ups agree for a single dataset
    assert res["systems"]["eng:m"]["all"]["ci"]["recipient"]["outcomes"] == rec


def test_score_uses_existing_verdict_and_does_not_queue_again(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1"], "population": {"all": 1}}}}
    refs = _refs([{"record_id": "r1", "gold_recipient": "the gym"}])
    jpath = tmp_path / "judgments.csv"
    _existing_csv(jpath, [{f: "" for f in scoring.JUDGMENT_FIELDS} | {
        "record_id": "r1", "dataset": "ds", "param": "recipient", "model_value": "some org", "reference": "the gym",
        "verdict": "partial", "models": "eng:m"}])
    res = scoring.score(sample, refs, [_run_dict({"r1": _full_rec("r1", {"recipient": "some org"})})], jpath, {})
    d = res["systems"]["eng:m"]["datasets"]["ds"]["ci"]["recipient"]
    assert d["outcomes"] == {"partial": 1} and d["accuracy_strict"] == 0.0 and d["accuracy_lenient"] == 1.0
    assert res["pending_judgments"] == 0
    assert _read(jpath)[0]["verdict"] == "partial"


def test_score_non_blind_run_excludes_benchmark_values_and_warns(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1"], "population": {"all": 1}}}}
    refs = _refs([{"record_id": "r1", "gold_recipient": "the gym"}])
    rec = _full_rec("r1", {"recipient": "the gym"}, origin_default="benchmark", native_ci={"recipient": {"value": "the gym"}})
    res = scoring.score(sample, refs, [_run_dict({"r1": rec}, blind=False)], tmp_path / "j.csv", {})
    assert res["systems"]["eng:m"]["datasets"]["ds"]["ci"]["recipient"]["outcomes"] == {"benchmark_value": 1}
    assert any("without --blind-native-ci" in w for w in res["warnings"])


def test_score_ignores_datasets_outside_sample_and_rejects_duplicate_runs(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1"], "population": {"all": 1}}}}
    refs = _refs([{"record_id": "r1", "gold_recipient": "the gym"}])
    good = _run_dict({"r1": _full_rec("r1", {"recipient": "the gym"})})
    res = scoring.score(sample, refs, [good, _run_dict({}, dataset="other")], tmp_path / "j.csv", {})
    assert list(res["systems"]["eng:m"]["datasets"]) == ["ds"] and any("not in the pilot sample" in w for w in res["warnings"])
    with pytest.raises(PipelineError, match="two runs for eng:m on ds"):
        scoring.score(sample, refs, [good, _run_dict({"r1": _full_rec("r1", {})})], tmp_path / "j.csv", {})


def test_score_warns_about_records_outside_the_sample(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1"], "population": {"all": 1}}}}
    refs = _refs([{"record_id": "r1", "gold_recipient": "the gym"}])
    run_ = _run_dict({"r1": _full_rec("r1", {"recipient": "the gym"}), "zzz": _full_rec("zzz", {"recipient": "x"})})
    res = scoring.score(sample, refs, [run_], tmp_path / "j.csv", {})
    assert any("1 record(s) outside the pilot sample" in w for w in res["warnings"])
    assert res["systems"]["eng:m"]["datasets"]["ds"]["validity"]["sampled"] == 1


def test_score_scores_privacy_only_where_reference_filled_in(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1", "r2", "r3"], "population": {"all": 3}}}}
    refs = _refs([{"record_id": "r1", "gold_privacy": "Appropriate"}, {"record_id": "r2", "gold_privacy": "inappropriate"},
                  {"record_id": "r3", "gold_privacy": "uncertain"}])
    ok = {i: _full_rec(i, {}) for i in ("r1", "r2", "r3")}
    priv = {"r1": {"prediction": {"label": "appropriate"}}, "r2": {"prediction": {"label": "appropriate"}}}
    res = scoring.score(sample, refs, [_run_dict(ok, privacy=priv)], tmp_path / "j.csv", {})
    p = res["systems"]["eng:m"]["datasets"]["ds"]["privacy"]
    assert p["outcomes"] == {"correct": 1, "incorrect": 1, "no_prediction": 1} and p["accuracy"] == round(1 / 3, 3)
    no_gold = scoring.score(sample, _refs([{"record_id": "r1"}]), [_run_dict(ok, privacy=priv)], tmp_path / "j2.csv", {})
    assert no_gold["systems"]["eng:m"]["datasets"]["ds"]["privacy"] is None


def test_score_efficiency_sums_tokens_and_wall_time_and_prices_api_cost(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1", "r2"], "population": {"all": 10}}}}
    refs = _refs([{"record_id": "r1"}, {"record_id": "r2"}])
    ok = {"r1": _full_rec("r1", {}, usage={"prompt_tokens": 1000, "completion_tokens": 100}, latency=2.0),
          "r2": _full_rec("r2", {}, usage={"prompt_tokens": 3000, "completion_tokens": 300}, latency=4.0)}
    cfg = {"costs": {"api_pricing": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 10.0, "priced_on": "2026-01-01"}}}}
    res = scoring.score(sample, refs, [_run_dict(ok)], tmp_path / "j.csv", cfg)
    e = res["systems"]["eng:m"]["datasets"]["ds"]["efficiency"]
    assert e["prompt_tokens"] == 4000 and e["completion_tokens"] == 400 and e["wall_s"] == 100.0
    assert e["latency_s_mean"] == 3.0 and e["latency_s_max"] == 4.0
    assert e["api_cost_usd"] == round(4000 / 1e6 * 1.0 + 400 / 1e6 * 10.0, 4)
    # extrapolation: population 10 x mean tokens (2000 / 200)
    assert e["full_dataset_estimate"]["prompt_tokens"] == 20000 and e["full_dataset_estimate"]["completion_tokens"] == 2000


def test_score_efficiency_token_totals_are_none_when_any_record_lacks_usage(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1", "r2"], "population": {"all": 2}}}}
    refs = _refs([{"record_id": "r1"}, {"record_id": "r2"}])
    ok = {"r1": _full_rec("r1", {}), "r2": _full_rec("r2", {}, usage={"prompt_tokens": None, "completion_tokens": None})}
    e = scoring.score(sample, refs, [_run_dict(ok)], tmp_path / "j.csv", {})["systems"]["eng:m"]["datasets"]["ds"]["efficiency"]
    assert e["prompt_tokens"] is None and e["completion_tokens"] is None and "api_cost_usd" not in e


def test_score_with_no_runs_is_empty_and_report_renders(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1"], "population": {"all": 1}}}}
    res = scoring.score(sample, _refs([{"record_id": "r1"}]), [], tmp_path / "j.csv", {})
    assert res["systems"] == {} and res["annotation_coverage"] == {"ds": 0}
    assert "No runs scored" in scoring.render_markdown(res)


def test_render_markdown_and_write_report_for_a_scored_run(tmp_path):
    sample = {"datasets": {"ds": {"record_ids": ["r1"], "population": {"all": 1}}}}
    refs = _refs([{"record_id": "r1", "gold_recipient": "the gym", "gold_domain": "Health"}])
    res = scoring.score(sample, refs, [_run_dict({"r1": _full_rec("r1", {"recipient": "the gym"})})], tmp_path / "j.csv", {})
    files = scoring.write_report(res, tmp_path / "out")
    assert json.loads(files["json"].read_text())["systems"].keys() == {"eng:m"}
    assert "eng:m" in files["md"].read_text() and "Pilot comparison report" in files["md"].read_text()


# =========================================================================== gpu.py
def _g(t, idx="0", util="50", mem="1024", total="81920", power="100", name="GPU-X"):
    return {"utc_epoch": float(t), "gpu_index": idx, "name": name, "util_pct": util, "mem_used_mib": mem, "mem_total_mib": total, "power_w": power}


def test_gpu_summarize_none_when_window_has_no_rows():
    rows = [_g(10), _g(11)]
    assert gpu.summarize(rows, 20, 30) is None
    assert gpu.summarize([], 0, 100) is None


def test_gpu_summarize_window_is_inclusive_and_ignores_outside_rows():
    rows = [_g(9, util="99"), _g(10, util="10"), _g(20, util="30"), _g(21, util="99")]
    s = gpu.summarize(rows, 10, 20)
    assert s["samples"] == 2 and s["mean_util_pct"] == 20.0


def test_gpu_summarize_aggregates_multiple_gpus_per_timestamp():
    rows = [_g(t, idx=i, util=u, mem="2048", power="150") for t in (100, 101) for i, u in (("0", "40"), ("1", "60"))]
    s = gpu.summarize(rows, 100, 101)
    assert s["n_gpus"] == 2 and s["samples"] == 2 and s["mean_util_pct"] == 50.0
    assert s["peak_mem_used_gib_all_gpus"] == 4.0  # 2 x 2048 MiB at the same instant
    assert s["mem_total_gib_per_gpu"] == 80.0
    assert s["mean_power_w_all_gpus"] == 300.0
    assert s["energy_wh"] == round(300.0 * 1 / 3600, 2) and s["gpu_names"] == ["GPU-X"]


def test_gpu_summarize_handles_not_available_power_and_util():
    rows = [_g(5, power="[N/A]"), _g(6, power="[N/A]", util="[N/A]")]
    s = gpu.summarize(rows, 5, 6)
    assert s["mean_util_pct"] == 50.0  # only the parseable util sample counts
    assert s["mean_power_w_all_gpus"] == 0.0  # no power reported: treated as zero draw
    assert s["energy_wh"] == 0.0


def test_gpu_summarize_all_util_unavailable_gives_none_mean():
    s = gpu.summarize([_g(5, util="[N/A]")], 0, 10)
    assert s["mean_util_pct"] is None and s["samples"] == 1


def test_gpu_read_log_roundtrip_converts_epoch_to_float(tmp_path):
    p = tmp_path / "gpu.csv"
    p.write_text(",".join(gpu.FIELDS) + "\n1700000000.5,0,0,GPU-X,10,100,1000,[N/A]\n", encoding="utf-8")
    (r,) = gpu.read_log(p)
    assert r["utc_epoch"] == 1700000000.5 and r["power_w"] == "[N/A]"
    assert gpu.summarize([r], 1700000000, 1700000001)["n_gpus"] == 1


# =========================================================================== call_and_parse spend accounting
class ScriptedEngine(Engine):
    """Replays ModelResponse objects (or raises EngineErrors) in order."""

    name = "scripted"
    retry_base_delay = 0.0

    def __init__(self, script):
        super().__init__("scripted-model")
        self.script = list(script)
        self.prompts: list[str] = []

    async def _generate_once(self, system, user, schema, settings):
        self.prompts.append(user)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(content, pt=10, ct=5, latency=1.0, finish="stop", usage=None):
    return ModelResponse(content=content, model="scripted-model", engine="scripted", finish_reason=finish,
                         usage={"prompt_tokens": pt, "completion_tokens": ct} if usage is None else usage, latency_s=latency, structured_mode="none")


BAD = '{"ci_parameters": {}}'
SETTINGS = GenerationSettings(retries=0)


async def test_call_and_parse_single_success_reports_one_call():
    eng = ScriptedEngine([_resp(valid_extraction_json(), 12, 7, 0.5)])
    inst, resp, attempts, raw = await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 2)
    assert attempts == 1 and isinstance(inst, LLMExtraction) and raw == valid_extraction_json()
    assert resp.usage == {"prompt_tokens": 12, "completion_tokens": 7, "calls": 1}  # no per_call for a single call
    assert resp.latency_s == 0.5


async def test_call_and_parse_sums_usage_and_latency_over_all_attempts():
    eng = ScriptedEngine([_resp(BAD, 10, 5, 1.25), _resp(valid_extraction_json(), 20, 7, 2.0)])
    _, resp, attempts, _ = await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 2)
    assert attempts == 2
    assert resp.usage["prompt_tokens"] == 30 and resp.usage["completion_tokens"] == 12 and resp.usage["calls"] == 2
    assert [c["prompt_tokens"] for c in resp.usage["per_call"]] == [10, 20]
    assert resp.latency_s == 3.25
    assert "previous reply was rejected" in eng.prompts[1] and "previous reply was rejected" not in eng.prompts[0]


async def test_call_and_parse_sum_is_none_when_any_call_lacks_token_counts():
    eng = ScriptedEngine([_resp(BAD, usage={}), _resp(valid_extraction_json(), 20, 7)])
    _, resp, _, _ = await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 2)
    assert resp.usage["prompt_tokens"] is None and resp.usage["completion_tokens"] is None and resp.usage["calls"] == 2


async def test_call_and_parse_latency_is_none_when_any_call_lacks_latency():
    eng = ScriptedEngine([_resp(BAD, latency=None), _resp(valid_extraction_json(), latency=1.0)])
    _, resp, _, _ = await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 2)
    assert resp.latency_s is None


async def test_call_and_parse_failure_after_retries_carries_usage_of_every_call():
    eng = ScriptedEngine([_resp("not json", 10, 5, 1.0), _resp(BAD, 11, 6, 2.0), _resp(BAD, 12, 7, 3.0)])
    with pytest.raises(_Fail) as ei:
        await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 2)
    f = ei.value
    assert f.attempts == 3 and f.etype == "InvalidModelOutput"
    u = f.spent.usage()
    assert u["prompt_tokens"] == 33 and u["completion_tokens"] == 18 and u["calls"] == 3
    assert f.spent.latency_s() == 6.0


async def test_call_and_parse_engine_error_on_retry_keeps_spend_of_earlier_calls():
    eng = ScriptedEngine([_resp(BAD, 10, 5, 1.0), EngineError("boom")])
    with pytest.raises(_Fail) as ei:
        await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 2)
    assert ei.value.stage == "api" and ei.value.attempts == 2
    assert ei.value.spent.usage()["prompt_tokens"] == 10 and ei.value.spent.usage()["calls"] == 1


async def test_call_and_parse_truncation_on_last_attempt_fails_with_spend():
    eng = ScriptedEngine([_resp("{", 10, 5, finish="length"), _resp("{", 10, 5, finish="length")])
    with pytest.raises(_Fail) as ei:
        await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 1)
    assert ei.value.etype == "TruncatedOutput" and ei.value.spent.usage()["completion_tokens"] == 10


async def test_call_and_parse_refusal_fails_immediately():
    eng = ScriptedEngine([_resp("no", 10, 5, finish="refusal"), _resp(valid_extraction_json())])
    with pytest.raises(_Fail) as ei:
        await call_and_parse(eng, "s", "u", LLMExtraction, SETTINGS, 2)
    assert ei.value.etype == "ModelRefusal" and ei.value.attempts == 1 and len(eng.script) == 1


def _source_record(native_ci=None) -> SourceRecord:
    return SourceRecord(
        record_id="confaide_tier2:2a-000", dataset="confaide_tier2", subset="2a", level="scenario", source_id="2a-000", source_revision="rev",
        source_files=["f"], content_sha256="x", original_text="[TEST FIXTURE] Pat's gym kept notes.", scenario_text="[TEST FIXTURE] Pat's gym kept notes.",
        native_reference=NativeReference(kind="continuous_human_rating", label=1.0), native_ci=native_ci or {},
    )


async def test_extract_record_metadata_usage_is_the_sum_over_attempts(builder):
    eng = ScriptedEngine([_resp(BAD, 10, 5, 1.0), _resp(valid_extraction_json(), 20, 7, 2.0)])
    rec = await extract_record(eng, _source_record(), builder, SETTINGS, load_taxonomy(ROOT), "run-1", validation_retries=2)
    m = rec.extraction_metadata
    assert m.attempts == 2 and m.usage["prompt_tokens"] == 30 and m.usage["completion_tokens"] == 12 and m.usage["calls"] == 2
    assert m.latency_s == 3.0


async def test_extract_record_failure_record_carries_usage_and_latency(builder):
    eng = ScriptedEngine([_resp(BAD, 10, 5, 1.0), _resp(BAD, 20, 7, 2.5)])
    with pytest.raises(ExtractionFailure) as ei:
        await extract_record(eng, _source_record(), builder, SETTINGS, load_taxonomy(ROOT), "run-1", validation_retries=1)
    f = ei.value.failure
    assert f.attempts == 2 and f.usage["prompt_tokens"] == 30 and f.usage["completion_tokens"] == 12 and f.usage["calls"] == 2
    assert f.latency_s == 3.5


# =========================================================================== blind mode
def _native_record() -> SourceRecord:
    return _source_record({"sender": NativeCIValue(value="BENCH-SENDER-VALUE", source_field="sender_field"),
                           "recipient": NativeCIValue(value="BENCH-RECIPIENT-VALUE", source_field="recipient_field", origin="benchmark_mapped")})


def test_blind_prompt_hides_benchmark_values_and_changes_version_and_sha(builder):
    rec = _native_record()
    normal = builder.render_extraction(rec, LLMExtraction)
    blind = builder.render_extraction(rec, LLMExtraction, blind_native_ci=True)
    assert "BENCH-SENDER-VALUE" in normal.user and "BENCH-RECIPIENT-VALUE" in normal.user
    assert "BENCH-SENDER-VALUE" not in blind.user + blind.system and "BENCH-RECIPIENT-VALUE" not in blind.user + blind.system
    assert "extract all five from the text" in blind.user
    assert blind.version == normal.version + BLIND_SUFFIX and blind.version.endswith("+blind_native_ci")
    assert not normal.version.endswith("+blind_native_ci")
    assert blind.sha256 != normal.sha256 and len(blind.sha256) == 64


def test_blind_prompt_for_record_without_native_ci_differs_only_in_identity(builder):
    rec = _source_record()
    normal = builder.render_extraction(rec, LLMExtraction)
    blind = builder.render_extraction(rec, LLMExtraction, blind_native_ci=True)
    assert normal.user == blind.user and normal.sha256 != blind.sha256  # same text, but runs must not be resumed across modes


def _llm_extraction(**values) -> LLMExtraction:
    d = empty_extraction()
    for k, v in values.items():
        d["ci_parameters"][k] = {"value": v, "evidence": [v], "missing_reason": None}
    return LLMExtraction.model_validate(d)


def test_merge_ci_default_mode_keeps_benchmark_values_over_llm():
    out = merge_ci(_llm_extraction(sender="LLM sender", subject="LLM subject"), _native_record())
    assert out["sender"].value == "BENCH-SENDER-VALUE" and out["sender"].origin == "benchmark"
    assert out["recipient"].value == "BENCH-RECIPIENT-VALUE" and out["recipient"].origin == "benchmark_mapped"
    assert out["subject"].value == "LLM subject" and out["subject"].origin == "llm"
    assert out["information_type"].value is None and out["information_type"].origin == "none"


def test_merge_ci_blind_mode_keeps_llm_values_even_when_record_has_native_ci():
    rec = _native_record()
    out = merge_ci(_llm_extraction(sender="LLM sender"), rec, blind_native_ci=True)
    assert out["sender"].value == "LLM sender" and out["sender"].origin == "llm"
    assert out["recipient"].value is None and out["recipient"].origin == "none"  # model said null; benchmark value not substituted
    assert all(p.origin in ("llm", "none") for p in out.values())
    assert set(out) == set(CI_PARAMS)
    assert rec.native_ci["sender"].value == "BENCH-SENDER-VALUE"  # benchmark values stay on the record


async def test_extract_record_blind_flows_through_prompt_metadata_and_merge(builder):
    eng = ScriptedEngine([_resp(valid_extraction_json(ci_parameters={
        **empty_extraction()["ci_parameters"], "sender": {"value": "Pat's gym", "evidence": ["Pat's gym"], "missing_reason": None}}))])
    rec = await extract_record(eng, _native_record(), builder, SETTINGS, load_taxonomy(ROOT), "run-1", blind_native_ci=True)
    assert "BENCH-SENDER-VALUE" not in eng.prompts[0]
    assert rec.extraction_metadata.prompt_version.endswith("+blind_native_ci")
    assert rec.ci_parameters["sender"].origin == "llm" and rec.ci_parameters["sender"].value == "Pat's gym"
    assert rec.native_ci["sender"].value == "BENCH-SENDER-VALUE"


# =========================================================================== RunSpec.record_ids
def _ids_spec(paths, engine, ids, **kw):
    return RunSpec(dataset="confaide_tier2", engine=engine, settings=GenerationSettings(retries=0), output_dir=paths.processed, subsets=["2a"],
                   concurrency=1, validation_retries=0, show_progress=False, record_ids=ids, **kw)


def _extraction_ids(result) -> list[str]:
    return [json.loads(line)["record_id"] for line in (result["run_dir"] / "extractions.jsonl").read_text().splitlines()]


def test_run_restricted_to_record_ids_processes_exactly_those_in_order(confaide_paths):
    eng = MockEngine()
    ids = ["confaide_tier2:2a-002", "confaide_tier2:2a-000"]
    res = run(_ids_spec(confaide_paths, eng, ids), confaide_paths)
    assert eng.calls == 2
    assert _extraction_ids(res) == ids  # requested order, not adapter order
    assert res["manifest"]["records_selected"] == 2 and res["manifest"]["record_ids_requested"] == 2


def test_run_record_ids_duplicates_are_processed_once(confaide_paths):
    eng = MockEngine()
    res = run(_ids_spec(confaide_paths, eng, ["confaide_tier2:2a-001", "confaide_tier2:2a-001"]), confaide_paths)
    assert eng.calls == 1 and _extraction_ids(res) == ["confaide_tier2:2a-001"]


def test_run_unknown_record_id_raises_before_any_inference_or_output(confaide_paths):
    eng = MockEngine()
    with pytest.raises(PipelineError, match="1 requested record id.*not produced"):
        run(_ids_spec(confaide_paths, eng, ["confaide_tier2:2a-000", "confaide_tier2:2a-999"]), confaide_paths)
    assert eng.calls == 0 and not (confaide_paths.processed / "confaide_tier2").exists()


def test_run_record_id_from_unselected_subset_is_unknown(confaide_paths):
    with pytest.raises(PipelineError, match="not produced"):
        run(_ids_spec(confaide_paths, MockEngine(), ["confaide_tier2:2b-000"]), confaide_paths)


def test_run_blind_native_ci_is_recorded_in_manifest_and_prompt_version(confaide_paths):
    res = run(_ids_spec(confaide_paths, MockEngine(), ["confaide_tier2:2a-000"], blind_native_ci=True), confaide_paths)
    assert res["manifest"]["blind_native_ci"] is True and res["manifest"]["prompt_version"].endswith("+blind_native_ci")


# =========================================================================== cli.read_ids_file
def test_read_ids_file_json_sample_format(tmp_path):
    p = tmp_path / "pilot_sample.json"
    p.write_text(json.dumps({"datasets": {"d1": {"record_ids": ["a", "b"]}, "d2": {"record_ids": ["c"]}}}))
    assert read_ids_file(p, "d1") == ["a", "b"] and read_ids_file(p, "d2") == ["c"]


def test_read_ids_file_json_without_dataset_or_with_empty_list_raises(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"datasets": {"d1": {"record_ids": []}}}))
    with pytest.raises(PipelineError, match="no record ids for dataset 'zzz'"):
        read_ids_file(p, "zzz")
    with pytest.raises(PipelineError, match="lists no record ids"):
        read_ids_file(p, "d1")
    p.write_text(json.dumps({}))
    with pytest.raises(PipelineError, match="no record ids for dataset"):
        read_ids_file(p, "d1")


def test_read_ids_file_plain_text_skips_blanks_and_comments_and_whitespace(tmp_path):
    p = tmp_path / "ids.txt"
    p.write_text("# pilot ids\nconfaide_tier2:2a-000\n\n   \n  confaide_tier2:2a-001  \n# trailing comment\n", encoding="utf-8")
    assert read_ids_file(p, "any_dataset") == ["confaide_tier2:2a-000", "confaide_tier2:2a-001"]


def test_read_ids_file_plain_text_only_comments_raises(tmp_path):
    p = tmp_path / "ids.txt"
    p.write_text("# nothing\n\n")
    with pytest.raises(PipelineError, match="lists no record ids"):
        read_ids_file(p, "d")



def test_gpu_summarize_groups_by_sample_not_rounded_second():
    # two samples of ONE 80 GB GPU inside the same second must not be summed into 160 GB
    rows = [{"utc_epoch": 10.1, "sample": "0", "gpu_index": "0", "name": "G", "util_pct": "50", "mem_used_mib": "70000",
             "mem_total_mib": "81920", "power_w": "300"},
            {"utc_epoch": 10.4, "sample": "1", "gpu_index": "0", "name": "G", "util_pct": "50", "mem_used_mib": "70000",
             "mem_total_mib": "81920", "power_w": "300"}]
    s = gpu.summarize(rows, 10, 11)
    assert s["samples"] == 2 and s["peak_mem_used_gib_all_gpus"] == round(70000 / 1024, 2) and s["mean_power_w_all_gpus"] == 300.0


def test_merge_judgments_keeps_extra_columns_and_short_rows(tmp_path):
    p = tmp_path / "judgments.csv"
    p.write_text("record_id,dataset,param,model_value,reference,verdict,token_f1,models,judged_by,note,second_opinion\n"
                 "r1,ds,recipient,the gym,a gym centre,correct,0.5,sys:a,ann,,KEEP-ME\n"
                 "r2,ds,sender,x\n", encoding="utf-8")  # short row, as Excel can leave behind
    pending = {scoring._jkey("r3", "subject", "you", "your friends"): {
        "record_id": "r3", "dataset": "ds", "param": "subject", "model_value": "you", "reference": "your friends",
        "verdict": "", "token_f1": 0.0, "models": {"sys:b"}}}
    scoring.merge_judgments(p, pending)
    rows = list(csv.DictReader(p.open()))
    assert rows[0]["second_opinion"] == "KEEP-ME" and rows[0]["verdict"] == "correct"
    assert [r["record_id"] for r in rows] == ["r1", "r2", "r3"]
    assert list(tmp_path.glob("judgments.csv.bak-*")), "previous file must be archived"


def test_score_warns_on_unrecognised_verdict(tmp_path):
    jp = tmp_path / "judgments.csv"
    jp.write_text("record_id,dataset,param,model_value,reference,verdict\nr1,ds,recipient,a,b,corect\n", encoding="utf-8")
    res = scoring.score({"datasets": {}}, [], [], jp, {})
    assert any("corect" in w for w in res["warnings"])


def test_load_run_combines_batch_sessions(tmp_path):
    # a run resumed in a second batch: wall time must cover both sessions, not only the last manifest
    d = tmp_path / "ds" / "vllm__m"
    d.mkdir(parents=True)
    base = {"dataset": "ds", "engine": {"engine": "vllm", "model": "m"}, "blind_native_ci": True}
    (d / "run_manifest.batch1.json").write_text(json.dumps({**base, "run_id": "a", "started_at": "2026-01-01T00:00:00+00:00",
                                                            "finished_at": "2026-01-01T00:01:40+00:00", "records_resumed_from_previous": 0}))
    last = {**base, "run_id": "b", "started_at": "2026-01-01T01:00:00+00:00", "finished_at": "2026-01-01T01:00:50+00:00",
            "records_resumed_from_previous": 5}
    (d / "run_manifest.json").write_text(json.dumps(last))
    (d / "run_manifest.batch2.json").write_text(json.dumps(last))  # duplicate copy of the last session: counted once
    (d / "extractions.jsonl").write_text("")
    run = scoring.load_run(d)
    assert len(run["sessions"]) == 2 and run["sessions_complete"]
    eff = scoring.efficiency(run, [], {}, {}, {}, [], None)
    assert eff["wall_s"] == 150.0 and eff["sessions"] == 2 and eff["wall_time_partial"] is False
    rows = [{"utc_epoch": t, "sample": str(i), "gpu_index": "0", "name": "G", "util_pct": "90", "mem_used_mib": "1024",
             "mem_total_mib": "2048", "power_w": "100"} for i, t in enumerate([run["sessions"][0][0] + 10, run["sessions"][1][0] + 10, 5.0])]
    g = gpu.summarize_windows(rows, run["sessions"])
    assert g["samples"] == 2 and g["energy_wh"] == round(100 * 150 / 3600, 2)


# =========================================================================== human review of model outputs
from ci_audit.evaluation import review


def test_review_pick_batch_prefers_shared_ids_and_spreads_subsets():
    sample = {"datasets": {"ds": {"subsets": ["a", "b"], "record_ids": [f"a{i}" for i in range(6)] + [f"b{i}" for i in range(6)]}}}
    subsets_of = {i: i[0] for i in sample["datasets"]["ds"]["record_ids"]}
    batch = review.pick_batch(sample, 5, seed=1, prefer={"b3"}, subsets_of=subsets_of)["ds"]
    assert batch[0] == "b3" and len(batch) == len(set(batch)) == 5
    assert Counter(subsets_of[i] for i in batch) == {"a": 3, "b": 2}  # quota 3/2, the preferred b3 counts for b
    assert batch == review.pick_batch(sample, 5, seed=1, prefer={"b3"}, subsets_of=subsets_of)["ds"]  # reproducible


def test_review_summarize_rates_and_unknown_labels(tmp_path):
    p = tmp_path / "sheet.csv"
    rows = [{"record_id": f"r{k}", "dataset": "ds", "subset": "a", "model": "m", "item": "sender", "judgment": j}
            for k, j in enumerate(["correct", "correct", "partial", "invented", "missed", "Corect", ""])]
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=review.SHEET_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    s = review.summarize([p])
    a = s["models"]["m"]["all"]
    assert a["judged"] == 5 and a["unjudged"] == 2  # the typo is reported and left unjudged, not guessed
    assert a["correct_rate"] == 0.4 and a["correct_or_partial_rate"] == 0.6 and a["invented"] == 1 and a["missed"] == 1
    assert len(s["problems"]) == 1 and "corect" in s["problems"][0]


def test_review_write_batch_refuses_to_overwrite(tmp_path):
    rec = {"record_id": "ds:1", "dataset": "ds", "subset": "a", "scenario_text": "[TEST FIXTURE] text",
           "ci_parameters": {p: {"value": None, "evidence": [], "missing_reason": "not stated"} for p in CI_PARAMS},
           "domain": {"domain": "Unclear", "subdomain": None, "supporting_span": None, "explanation": "x"}, "validation_warnings": []}
    run = tmp_path / "run"
    run.mkdir()
    (run / "extractions.jsonl").write_text(json.dumps(rec) + "\n")
    files = review.write_batch({"ds": run}, {"ds": ["ds:1"]}, "m", "me", tmp_path / "out")
    assert len(list(csv.DictReader(files["sheet"].open()))) == 6  # five CI parameters + domain
    with pytest.raises(FileExistsError):
        review.write_batch({"ds": run}, {"ds": ["ds:1"]}, "m", "me", tmp_path / "out")
