import json

import pytest
from pydantic import ValidationError

from ci_audit.engines.mock import empty_extraction
from ci_audit.processing.domain_classification import build_inventory, finalize_domain, write_inventory
from ci_audit.processing.validation import (ResponseParseError, check_extraction, evidence_in_source, merge_ci, parse_json_response, validate_model)
from ci_audit.prompts import load_taxonomy
from ci_audit.schemas import (DomainPrediction, LLMExtraction, NativeCIValue, NativeReference, SourceRecord, strict_json_schema)
from conftest import ROOT

SRC = "[TEST FIXTURE] Information about your pets is collected by your imaginary gym in order to plan classes."


def make_ex(**patch) -> LLMExtraction:
    d = empty_extraction()
    for k, v in patch.items():
        d[k] = v
    return LLMExtraction.model_validate(d)


def record(native_ci=None) -> SourceRecord:
    return SourceRecord(record_id="x:1", dataset="x", source_id="1", source_revision="r", source_files=["f"], content_sha256="h",
                        original_text=SRC, scenario_text=SRC, native_reference=NativeReference(kind="none"), native_ci=native_ci or {})


# ------------------------------------------------------------------ schema
def test_strict_schema_has_no_open_objects_and_all_required():
    s = strict_json_schema(LLMExtraction)

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object" and "properties" in n:
                assert n["additionalProperties"] is False
                assert set(n["required"]) == set(n["properties"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(s)
    assert "default" not in json.dumps(s)


def test_missing_ci_parameters_are_valid_nulls():
    ex = make_ex()
    assert ex.ci_parameters.sender.value is None and ex.ci_parameters.sender.missing_reason


def test_schema_rejects_missing_or_wrong_fields():
    d = empty_extraction()
    del d["ci_parameters"]["recipient"]
    _, errs = validate_model(d, LLMExtraction)
    assert errs and any("recipient" in e for e in errs)
    d2 = empty_extraction()
    d2["domain"]["confidence"] = "very sure"
    assert validate_model(d2, LLMExtraction)[1]


# ------------------------------------------------------------------ parsing invalid responses
@pytest.mark.parametrize("text,ok", [
    ('{"a": 1}', True),
    ('```json\n{"a": 1}\n```', True),
    ('<think>hmm {"x":0}</think>\n{"a": 1}', True),
    ('Sure! Here you go: {"a": 1} hope it helps', True),
    ("", False), ("no json here", False), ('{"a": 1', False), ("[1, 2]", False),
])
def test_parse_json_response(text, ok):
    if ok:
        assert parse_json_response(text) == {"a": 1}
    else:
        with pytest.raises(ResponseParseError):
            parse_json_response(text)


# ------------------------------------------------------------------ evidence checks
def test_evidence_matching_is_whitespace_case_quote_insensitive():
    assert evidence_in_source("Your  Pets", SRC)
    assert evidence_in_source("pets ... plan classes", SRC)
    assert not evidence_in_source("your cats", SRC)
    assert not evidence_in_source("", SRC)


def test_unsupported_permission_and_relationship_are_dropped_not_kept():
    ex = make_ex(context={**empty_extraction()["context"],
                          "explicit_permissions": [{"text": "user consented", "evidence": "the user consented"}],
                          "explicit_restrictions": [{"text": "classes only", "evidence": "plan classes"}],
                          "relationships": [{"actors": ["you", "gym"], "relation": "member", "evidence": "you are a member"}]})
    ex, warns = check_extraction(ex, SRC)
    assert ex.context.explicit_permissions == [] and len(ex.context.explicit_restrictions) == 1 and ex.context.relationships == []
    assert sum("DROPPED" in w for w in warns) == 2


def test_value_without_evidence_and_null_without_reason_are_flagged():
    d = empty_extraction()
    d["ci_parameters"]["recipient"] = {"value": "your imaginary gym", "evidence": [], "missing_reason": None}
    d["ci_parameters"]["sender"]["missing_reason"] = None
    d["ci_parameters"]["subject"] = {"value": "your pets", "evidence": ["your hamsters"], "missing_reason": None}
    _, warns = check_extraction(LLMExtraction.model_validate(d), SRC)
    assert any("recipient: value without evidence" in w for w in warns)
    assert any("sender: null without missing_reason" in w for w in warns)
    assert any("subject: evidence not found" in w for w in warns)


def test_benchmark_ci_values_are_never_overwritten_by_llm():
    d = empty_extraction()
    d["ci_parameters"]["recipient"] = {"value": "LLM SAYS THIS", "evidence": ["your imaginary gym"], "missing_reason": None}
    d["ci_parameters"]["sender"] = {"value": "LLM sender", "evidence": ["your imaginary gym"], "missing_reason": None}
    rec = record({"recipient": NativeCIValue(value="benchmark recipient", source_field="seed.data_recipient")})
    merged = merge_ci(LLMExtraction.model_validate(d), rec)
    assert merged["recipient"].value == "benchmark recipient" and merged["recipient"].origin == "benchmark"
    assert merged["sender"].value == "LLM sender" and merged["sender"].origin == "llm"
    assert merged["subject"].origin == "none"


# ------------------------------------------------------------------ domain parsing + inventory
TAX = load_taxonomy(ROOT)


def pred(domain, conf="high", span="your imaginary gym", needs=False, sub=None):
    return DomainPrediction(domain=domain, subdomain=sub, explanation="because", supporting_span=span, confidence=conf, needs_review=needs)


def test_domain_canonicalises_case_but_keeps_new_labels():
    assert finalize_domain(pred("healthcare"), TAX).domain == "Healthcare"
    f = finalize_domain(pred("Library Services"), TAX)
    assert f.domain == "Library Services" and not f.in_taxonomy and any("candidate new domain" in r for r in f.review_reasons)


def test_domain_review_reasons():
    assert finalize_domain(pred("Education"), TAX).review_reasons == []
    assert "low confidence" in finalize_domain(pred("Education", conf="low"), TAX).review_reasons
    u = finalize_domain(pred("Unclear", span=None), TAX)
    assert u.in_taxonomy and "domain unclear" in u.review_reasons
    assert "no supporting span" in finalize_domain(pred("Education", span=None), TAX).review_reasons


def test_inventory_counts_and_outputs(tmp_path):
    def rec(ds, dom, **kw):
        fd = finalize_domain(pred(dom, **kw), TAX).model_dump()
        return {"record_id": f"{ds}:{dom}:{kw.get('conf', 'h')}:{id(kw)}", "dataset": ds, "domain": fd, "scenario_text": "t"}

    recs = [rec("a", "Education"), rec("a", "Education"), rec("b", "education", conf="low"), rec("b", "Unclear", span=None), rec("b", "Library Services")]
    inv = build_inventory(recs, TAX)
    by = {e["domain"]: e for e in inv["domains"]}
    assert by["Education"]["n_total"] == 3 and by["Education"]["by_dataset"] == {"a": 2, "b": 1}
    assert by["Education"]["n_uncertain"] == 1 and by["Unclear"]["n_uncertain"] == 1
    assert inv["candidate_new_domains"] == ["Library Services"]
    assert len(inv["review_list"]) == 3
    files = write_inventory(inv, tmp_path)
    assert all(p.exists() for p in files.values())
    assert json.loads(files["json"].read_text())["n_records"] == 5
    assert files["csv"].read_text().splitlines()[0].startswith("domain,in_taxonomy,n_total")
