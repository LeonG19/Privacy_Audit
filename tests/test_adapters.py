import pytest

from ci_audit.datasets import ADAPTERS, get_adapter_class
from ci_audit.datasets.base import AdapterError
from ci_audit.datasets.confaide import ConfaideTier2Adapter, T2_INSTRUCTION
from ci_audit.downloader import verify_source
from conftest import FIXTURE_2A, FIXTURE_2B, FIXTURE_LABELS, make_confaide_fixture


# ----------------------------------------------------------------- fixture-based (invented data)
def test_tier2_fixture_loads_both_variants(confaide_paths):
    recs = list(ConfaideTier2Adapter(confaide_paths, source_revision="fixture").iter_records())
    assert len(recs) == 6
    assert [r.subset for r in recs] == ["2a"] * 3 + ["2b"] * 3


def test_record_ids_are_stable_and_unique(confaide_paths):
    a = [r.record_id for r in ConfaideTier2Adapter(confaide_paths).iter_records()]
    b = [r.record_id for r in ConfaideTier2Adapter(confaide_paths).iter_records()]
    assert a == b and len(set(a)) == len(a)
    assert a[0] == "confaide_tier2:2a-000" and a[3] == "confaide_tier2:2b-000"


def test_content_hash_changes_when_text_changes(confaide_paths, tmp_path):
    h1 = [r.content_sha256 for r in ConfaideTier2Adapter(confaide_paths).iter_records()]
    bench = confaide_paths.raw / "confaide" / "github" / "benchmark" / "tier_2a.txt"
    bench.write_text(bench.read_text().replace("pets", "cats"), encoding="utf-8")
    h2 = [r.content_sha256 for r in ConfaideTier2Adapter(confaide_paths).iter_records()]
    assert h1[0] != h2[0] and h1[1:] == h2[1:]


def test_label_alignment_is_positional_and_unmodified(confaide_paths):
    recs = list(ConfaideTier2Adapter(confaide_paths).iter_records())
    for r in recs:
        i = int(r.source_id.split("-")[1])
        assert r.native_reference.label == float(FIXTURE_LABELS[i])
        assert r.native_reference.kind == "continuous_human_rating"  # never converted to binary
        assert r.native_reference.scale["min"] == -100 and r.native_reference.scale["max"] == 100
    r2a, r2b = recs[0], recs[3]
    assert r2a.links["paired_record_id"] == r2b.record_id and r2a.native_reference.label == r2b.native_reference.label


def test_original_text_preserved_exactly_and_instruction_separated(confaide_paths):
    r = next(iter(ConfaideTier2Adapter(confaide_paths).iter_records()))
    assert r.original_text == f"{T2_INSTRUCTION} \\n{FIXTURE_2A[0]}"  # literal backslash-n, trailing space as in file
    assert r.scenario_text == FIXTURE_2A[0]
    assert "privacy expectations" not in r.scenario_text
    assert r.native_fields["elicitation_prompt"] == T2_INSTRUCTION


def test_label_count_mismatch_is_refused_not_guessed(paths):
    make_confaide_fixture(paths.data, labels=FIXTURE_LABELS[:2])
    with pytest.raises(AdapterError, match="Refusing to guess"):
        list(ConfaideTier2Adapter(paths).iter_records())


def test_non_numeric_label_is_an_error(paths):
    make_confaide_fixture(paths.data, labels=["1.0", "oops", "2.0"])
    with pytest.raises(AdapterError, match="non-numeric"):
        list(ConfaideTier2Adapter(paths).iter_records())


def test_unexpected_instruction_is_flagged(confaide_paths):
    _, scenario, warns = ConfaideTier2Adapter.split_instruction("Some other prompt \\nbody text")
    assert scenario == "body text" and any("differs" in w for w in warns)
    _, scenario, warns = ConfaideTier2Adapter.split_instruction("no separator at all")
    assert scenario == "no separator at all" and warns


def test_missing_raw_files_give_actionable_error(paths):
    with pytest.raises(AdapterError, match="python -m ci_audit download --dataset confaide"):
        list(ConfaideTier2Adapter(paths).iter_records())


def test_unknown_dataset_and_subset_errors(paths):
    with pytest.raises(AdapterError, match="Did you mean 'confaide_tier2'"):
        get_adapter_class("confaide_tier_2")
    with pytest.raises(AdapterError, match="Unknown subset"):
        ConfaideTier2Adapter(paths, ["3z"])


# ----------------------------------------------------------------- real downloaded data
@pytest.mark.real_data
def test_real_tier2_counts_and_alignment(real_paths):
    a = ConfaideTier2Adapter(real_paths)
    recs = list(a.iter_records())
    assert len(recs) == 196
    first = recs[0]
    assert first.native_reference.label == 1.26 and first.scenario_text.startswith("Information about your friends")
    assert not any(r.parse_warnings for r in recs)
    chk = a.check()
    assert not chk["problems"] and chk["alignment_check"]["above_median"] == 98


@pytest.mark.real_data
def test_real_adapters_load_and_do_not_modify_raw(real_paths):
    expected = {"confaide_tier2": 196, "confaide_tier3": 270, "privacylens": 1479, "mpci_bench": 6156, "airgapagent_r": 4160, "pisas": 265}
    for name, n in expected.items():
        chk = get_adapter_class(name)(real_paths).check()
        assert chk["counts"]["records"] == n, name
        assert not chk["problems"], (name, chk["problems"])
    for src in ("confaide", "privacylens", "mpci_bench", "airgapagent_r", "pisas"):
        assert verify_source(real_paths, src)["problems"] == []


@pytest.mark.real_data
def test_real_mpci_pairs_and_missing_images_recorded(real_paths):
    recs = list(get_adapter_class("mpci_bench")(real_paths, ["story"]).iter_records())
    pos = next(r for r in recs if r.source_id.endswith("_pos:story"))
    assert pos.native_reference.label == "appropriate" and pos.links["paired_case"].endswith("_neg")
    assert pos.assets["image_required"] and pos.assets["images"][0]["available"] is False and not pos.assets["all_available"]
    assert pos.native_domain


@pytest.mark.real_data
def test_real_privacylens_levels_linked_and_not_called_appropriate(real_paths):
    recs = [r for r in get_adapter_class("privacylens")(real_paths).iter_records() if r.links["case_id"] == "main1"]
    assert {r.level for r in recs} == {"seed", "vignette", "trajectory"}
    assert all(r.native_reference.kind == "violation_by_construction" for r in recs)
    assert recs[0].links["vignette_record_id"] == "privacylens:main1:vignette"
    assert recs[0].native_ci["information_type"].origin == "benchmark"


@pytest.mark.real_data
def test_real_pisas_keeps_attribute_labels_separate(real_paths):
    recs = list(get_adapter_class("pisas")(real_paths).iter_records())
    assert {r.subset for r in recs} == {"original", "expanded"} and sum(r.subset == "original" for r in recs) == 85
    r = recs[0]
    assert isinstance(r.native_reference.label, dict) and r.native_reference.details["scenario_level_label"] is None
    assert "visibility" in r.source_metadata and "oracle_answer" in r.source_metadata["utility"]
    assert "oracle" not in r.scenario_text.lower()


@pytest.mark.real_data
def test_real_airgap_labels_match_matrix_and_nothing_invented(real_paths):
    r = next(iter(get_adapter_class("airgapagent_r")(real_paths).iter_records()))
    assert set(r.native_ci) == {"information_type"}  # no sender/recipient/principle fabricated
    assert r.text_is_rendered and r.native_reference.label in (0, 1)
