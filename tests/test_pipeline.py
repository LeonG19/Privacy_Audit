import json

import openai
import pytest

from ci_audit.engines.base import GenerationSettings
from ci_audit.engines.mock import MockEngine, empty_extraction
from ci_audit.pipeline import PipelineError, RunSpec, run, run_dir
from ci_audit.storage import JsonlWriter, archive_existing, read_jsonl
from conftest import FIXTURE_2A, FIXTURE_LABELS, valid_extraction_json


def spec(paths, engine=None, **kw):
    return RunSpec(dataset="confaide_tier2", engine=engine or MockEngine(), settings=GenerationSettings(retries=0), output_dir=paths.processed,
                   subsets=["2a"], concurrency=2, validation_retries=1, show_progress=False, **kw)


def good_response_for(system, user):
    d = empty_extraction()
    d["ci_parameters"]["recipient"] = {"value": "your imaginary gym", "evidence": ["your imaginary gym"], "missing_reason": None}
    d["domain"] = {"domain": "Education", "subdomain": None, "explanation": "x", "supporting_span": "your imaginary gym", "confidence": "high", "needs_review": False}
    return json.dumps(d)


# ------------------------------------------------------------------ output storage
def test_mock_run_writes_all_outputs_with_full_provenance(confaide_paths):
    eng = MockEngine(responder=good_response_for)
    res = run(spec(confaide_paths, eng), confaide_paths)
    rd = res["run_dir"]
    assert rd == run_dir(confaide_paths.processed, "confaide_tier2", "mock", "mock-model")
    recs = list(read_jsonl(rd / "extractions.jsonl"))
    assert len(recs) == 3 and (rd / "extractions.csv").exists() and (rd / "run_manifest.json").exists()
    r = recs[0]
    assert r["original_text"].startswith("Please indicate") and r["scenario_text"] == FIXTURE_2A[0]       # original preserved
    assert r["native_reference"]["label"] == float(FIXTURE_LABELS[0])                                      # native label kept
    assert r["ci_parameters"]["recipient"]["origin"] == "llm" and r["ci_parameters"]["sender"]["origin"] == "none"
    assert r["researcher_review"]["status"] == "unreviewed" and "model_privacy_prediction" not in r
    meta = r["extraction_metadata"]
    for k in ("model", "engine", "prompt_version", "prompt_sha256", "generation", "processed_at", "run_id", "status"):
        assert meta[k] not in (None, "")
    assert meta["is_mock"] is True and r["source_revision"] and r["content_sha256"]
    man = json.loads((rd / "run_manifest.json").read_text())
    assert man["generation"]["temperature"] == 0.0 and man["is_mock"] and man["records_selected"] == 3 and man["adapter_status"] == "complete"
    csv_head = (rd / "extractions.csv").read_text().splitlines()[0]
    assert "recipient_origin" in csv_head and "domain_needs_review" in csv_head


def test_limit_and_order(confaide_paths):
    res = run(spec(confaide_paths, limit=2), confaide_paths)
    assert [r["source_id"] for r in read_jsonl(res["run_dir"] / "extractions.jsonl")] == ["2a-000", "2a-001"] or res["manifest"]["records_selected"] == 2


# ------------------------------------------------------------------ invalid responses
def test_invalid_responses_go_to_failures_with_raw_output(confaide_paths):
    eng = MockEngine(responder=lambda s, u: "I cannot help with that")
    res = run(spec(confaide_paths, eng), confaide_paths)
    rd = res["run_dir"]
    assert not list(read_jsonl(rd / "extractions.jsonl"))
    fails = list(read_jsonl(rd / "failures.jsonl"))
    assert len(fails) == 3 and fails[0]["raw_response"] == "I cannot help with that" and fails[0]["stage"] in ("parse", "validation")
    assert fails[0]["attempts"] == 2  # 1 + validation_retries
    assert len(res["manifest"]["unresolved_failures"]) == 3


def test_validation_retry_feeds_back_errors_then_succeeds(confaide_paths):
    seen = []

    def responder(system, user):
        seen.append(user)
        return '{"ci_parameters": {}}' if len(seen) == 1 else good_response_for(system, user)

    res = run(spec(confaide_paths, MockEngine(responder=responder), limit=1), confaide_paths)
    rec = next(read_jsonl(res["run_dir"] / "extractions.jsonl"))
    assert rec["extraction_metadata"]["attempts"] == 2 and "previous reply was rejected" in seen[1] and "previous reply was rejected" not in seen[0]


def test_schema_valid_but_unsupported_claims_are_flagged_in_record(confaide_paths):
    d = empty_extraction()
    d["context"]["explicit_permissions"] = [{"text": "user consented", "evidence": "the user gave consent"}]
    res = run(spec(confaide_paths, MockEngine(responder=lambda s, u: json.dumps(d)), limit=1), confaide_paths)
    rec = next(read_jsonl(res["run_dir"] / "extractions.jsonl"))
    assert rec["context"]["explicit_permissions"] == [] and rec["extraction_metadata"]["status"] == "ok_with_warnings"
    assert any("DROPPED" in w for w in rec["validation_warnings"])
    assert "the user gave consent" in rec["extraction_metadata"]["raw_response"]  # the original claim stays auditable


# ------------------------------------------------------------------ resume / force
class Counting(MockEngine):
    def __init__(self):
        super().__init__(responder=good_response_for)


def test_resume_skips_completed_and_extends(confaide_paths):
    e1 = Counting()
    run(spec(confaide_paths, e1, limit=2), confaide_paths)
    assert e1.calls == 2
    e2 = Counting()
    res = run(spec(confaide_paths, e2, resume=True), confaide_paths)
    assert e2.calls == 1  # only the third record
    recs = list(read_jsonl(res["run_dir"] / "extractions.jsonl"))
    assert sorted(r["source_id"] for r in recs) == ["2a-000", "2a-001", "2a-002"]
    assert res["manifest"]["records_resumed_from_previous"] == 2


def test_resume_retries_previous_failures(confaide_paths):
    run(spec(confaide_paths, MockEngine(responder=lambda s, u: "garbage"), limit=2), confaide_paths)
    e = Counting()
    res = run(spec(confaide_paths, e, resume=True, limit=2), confaide_paths)
    assert e.calls == 2 and res["manifest"]["unresolved_failures"] == []
    assert any(p.name.startswith("failures.jsonl.bak-") for p in res["run_dir"].iterdir())  # old log archived, not deleted


def test_existing_outputs_require_resume_or_force(confaide_paths):
    run(spec(confaide_paths, Counting(), limit=1), confaide_paths)
    with pytest.raises(PipelineError, match="--resume.*--force"):
        run(spec(confaide_paths, Counting(), limit=1), confaide_paths)


def test_force_archives_old_output_and_reprocesses(confaide_paths):
    run(spec(confaide_paths, Counting(), limit=1), confaide_paths)
    e = Counting()
    res = run(spec(confaide_paths, e, limit=1, force=True), confaide_paths)
    assert e.calls == 1
    assert len(list(res["run_dir"].glob("extractions.jsonl.bak-*"))) == 1
    assert len(list(read_jsonl(res["run_dir"] / "extractions.jsonl"))) == 1


def test_resume_refuses_when_prompt_changed(confaide_paths):
    res = run(spec(confaide_paths, Counting(), limit=1), confaide_paths)
    f = res["run_dir"] / "extractions.jsonl"
    r = json.loads(f.read_text().splitlines()[0])
    r["extraction_metadata"]["prompt_sha256"] = "0" * 64
    f.write_text(json.dumps(r) + "\n")
    with pytest.raises(PipelineError, match="prompt templates changed"):
        run(spec(confaide_paths, Counting(), resume=True), confaide_paths)


def test_fatal_auth_error_aborts_run_but_keeps_completed(confaide_paths):
    from ci_audit.engines.base import AuthenticationFailed

    n = {"i": 0}

    def responder(s, u):
        n["i"] += 1
        return good_response_for(s, u) if n["i"] == 1 else AuthenticationFailed("key revoked")

    r = RunSpec(dataset="confaide_tier2", engine=MockEngine(responder=responder), settings=GenerationSettings(retries=0), output_dir=confaide_paths.processed,
                subsets=["2a"], concurrency=1, validation_retries=0, show_progress=False)
    with pytest.raises(PipelineError, match="aborted.*AuthenticationFailed"):
        run(r, confaide_paths)
    assert len(list(read_jsonl(run_dir(confaide_paths.processed, "confaide_tier2", "mock", "mock-model") / "extractions.jsonl"))) == 1


# ------------------------------------------------------------------ privacy classification (separate)
def test_privacy_prompt_never_contains_native_label_or_extraction(confaide_paths, builder):
    from ci_audit.datasets.confaide import ConfaideTier2Adapter
    from ci_audit.schemas import PrivacyPrediction

    rec = next(iter(ConfaideTier2Adapter(confaide_paths).iter_records()))
    assert rec.native_reference.label == 77.77
    p = builder.render_privacy(rec, PrivacyPrediction)
    blob = p.system + p.user
    assert "77.77" not in blob and "continuous_human_rating" not in blob and rec.record_id not in blob and "privacy expectations" not in blob
    assert FIXTURE_2A[0] in p.user


def test_privacy_run_writes_separate_file_and_leaves_extractions_untouched(confaide_paths):
    run(spec(confaide_paths, Counting(), limit=1), confaide_paths)
    pred = {"label": "uncertain", "rationale": "r", "key_factors": ["k"], "confidence": "low"}
    res = run(spec(confaide_paths, MockEngine(responder=lambda s, u: json.dumps(pred)), limit=1, operation="privacy"), confaide_paths)
    rd = res["run_dir"]
    p = next(read_jsonl(rd / "privacy_predictions.jsonl"))
    assert p["kind"] == "model_prediction" and p["prediction"]["label"] == "uncertain" and "native_reference" not in p
    ext = next(read_jsonl(rd / "extractions.jsonl"))
    assert "prediction" not in ext and ext["native_reference"]["label"] == 77.77


def test_privacy_classification_refused_for_unsupported_dataset(paths):
    s = RunSpec(dataset="pisas", engine=MockEngine(), settings=GenerationSettings(), output_dir=paths.processed, operation="privacy", show_progress=False)
    with pytest.raises(PipelineError, match="only implemented for confaide_tier2"):
        run(s, paths)


def test_vllm_down_fails_before_any_output(confaide_paths):
    from ci_audit.engines.base import ServerUnavailable
    from ci_audit.engines.vllm import VLLMEngine
    from conftest import FakeOpenAI

    eng = VLLMEngine("m", client=FakeOpenAI(models_exc=openai.APIConnectionError(request=__import__("httpx").Request("GET", "http://x"))), base_url="http://x/v1")
    with pytest.raises(ServerUnavailable):
        run(spec(confaide_paths, eng), confaide_paths)
    assert not (confaide_paths.processed / "confaide_tier2").exists()


# ------------------------------------------------------------------ storage primitives
def test_jsonl_roundtrip_and_truncated_tail_tolerated(tmp_path):
    p = tmp_path / "a.jsonl"
    with JsonlWriter(p) as w:
        w.write({"a": 1, "t": "ü"})
        w.write({"a": 2})
    with open(p, "a") as f:
        f.write('{"a": 3, "tru')  # simulated crash mid-write
    assert [r["a"] for r in read_jsonl(p)] == [1, 2]


def test_jsonl_corruption_in_the_middle_is_not_swallowed(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"a":1}\nNOT JSON\n{"a":3}\n')
    with pytest.raises(json.JSONDecodeError):
        list(read_jsonl(p))


def test_archive_existing_moves_not_deletes(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("data")
    q = archive_existing(p)
    assert not p.exists() and q.read_text() == "data" and archive_existing(p) is None
