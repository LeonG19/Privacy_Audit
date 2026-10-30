import anthropic
import httpx
import openai
import pytest

from ci_audit.engines import ENGINE_NAMES, create_engine
from ci_audit.engines.anthropic_api import AnthropicEngine
from ci_audit.engines.base import (AuthenticationFailed, BadRequest, EngineError, GenerationSettings, ModelNotFound, ServerUnavailable, TransientError)
from ci_audit.engines.mock import MockEngine
from ci_audit.engines.openai_api import OpenAIChatEngine
from ci_audit.engines.vllm import VLLMEngine
from ci_audit.schemas import LLMExtraction
from conftest import FakeAnthropic, FakeOpenAI, anthropic_response, chat_response, http_response, valid_extraction_json

S = GenerationSettings(retries=2)
REQ = httpx.Request("POST", "http://test.invalid/v1/x")


def oa_err(cls, status):
    return cls("boom", response=http_response(status), body={"message": "boom", "param": None})


def fast(engine):
    engine.retry_base_delay = 0.0
    return engine


# ------------------------------------------------------------------ selection
def test_engine_selection(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    assert isinstance(create_engine("vllm", "m", base_url="http://x/v1"), VLLMEngine)
    assert isinstance(create_engine("openai", "m"), OpenAIChatEngine) and not isinstance(create_engine("openai", "m"), VLLMEngine)
    assert isinstance(create_engine("anthropic", "m"), AnthropicEngine)
    assert isinstance(create_engine("mock", None), MockEngine)
    assert set(ENGINE_NAMES) >= {"vllm", "openai", "anthropic"}


def test_unknown_engine_has_helpful_message():
    with pytest.raises(EngineError, match="Did you mean 'anthropic'"):
        create_engine("anthropik", "m")


def test_model_required_for_hosted_engines_and_never_hardcoded(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with pytest.raises(EngineError, match="--model is required"):
        create_engine("openai", None)


# ------------------------------------------------------------------ authentication
def test_missing_api_keys_fail_fast_without_leaking(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AuthenticationFailed, match="OPENAI_API_KEY"):
        create_engine("openai", "m")
    with pytest.raises(AuthenticationFailed, match="ANTHROPIC_API_KEY"):
        create_engine("anthropic", "m")


async def test_openai_auth_rejected_is_not_retried_and_hides_key():
    secret = "sk-SECRET-VALUE-123"
    client = FakeOpenAI([oa_err(openai.AuthenticationError, 401)] * 3)
    e = fast(OpenAIChatEngine("m", client=client, api_key=secret))
    with pytest.raises(AuthenticationFailed) as ei:
        await e.generate("s", "u", None, S)
    assert len(client.calls) == 1 and secret not in str(ei.value) and secret not in repr(e.describe())


async def test_anthropic_auth_rejected_is_not_retried():
    client = FakeAnthropic([oa_err(anthropic.AuthenticationError, 401)] * 3)
    e = fast(AnthropicEngine("m", client=client))
    with pytest.raises(AuthenticationFailed):
        await e.generate("s", "u", None, S)
    assert len(client.calls) == 1


# ------------------------------------------------------------------ vLLM server failures
async def test_vllm_server_down_message_points_to_serve_script():
    client = FakeOpenAI(models_exc=openai.APIConnectionError(request=REQ))
    e = VLLMEngine("m", client=client, base_url="http://127.0.0.1:1/v1")
    with pytest.raises(ServerUnavailable, match="serve_vllm.sh"):
        await e.verify()


async def test_vllm_unknown_model_lists_served_models():
    e = VLLMEngine("not-served", client=FakeOpenAI(models=["served-a", "served-b"]), base_url="http://x/v1")
    with pytest.raises(ModelNotFound, match="served-a"):
        await e.verify()


async def test_vllm_uses_servers_only_model_when_unspecified_but_refuses_ambiguity():
    e = VLLMEngine(None, client=FakeOpenAI(models=["only-one"]), base_url="http://x/v1")
    await e.verify()
    assert e.model == "only-one"
    with pytest.raises(ModelNotFound, match="exposes 2 models"):
        await VLLMEngine(None, client=FakeOpenAI(models=["a", "b"]), base_url="http://x/v1").verify()


async def test_vllm_server_dies_mid_run_raises_after_retries():
    client = FakeOpenAI([openai.APIConnectionError(request=REQ)] * 3)
    e = fast(VLLMEngine("m", client=client, base_url="http://x/v1"))
    with pytest.raises(ServerUnavailable):
        await e.generate("s", "u", None, S)
    assert len(client.calls) == 3  # initial + 2 retries


# ------------------------------------------------------------------ retry behaviour
async def test_transient_errors_are_retried_then_succeed():
    client = FakeOpenAI([oa_err(openai.RateLimitError, 429), oa_err(openai.InternalServerError, 500), chat_response("{}")])
    e = fast(OpenAIChatEngine("m", client=client, api_key="k"))
    r = await e.generate("s", "u", None, S)
    assert r.content == "{}" and r.attempts == 3 and len(client.calls) == 3


async def test_retries_exhausted_raises_last_error():
    client = FakeOpenAI([oa_err(openai.RateLimitError, 429)] * 5)
    e = fast(OpenAIChatEngine("m", client=client, api_key="k"))
    with pytest.raises(TransientError):
        await e.generate("s", "u", None, GenerationSettings(retries=1))
    assert len(client.calls) == 2


async def test_zero_retries_means_one_attempt():
    client = FakeOpenAI([oa_err(openai.InternalServerError, 500)] * 2)
    with pytest.raises(TransientError):
        await fast(OpenAIChatEngine("m", client=client, api_key="k")).generate("s", "u", None, GenerationSettings(retries=0))
    assert len(client.calls) == 1


# ------------------------------------------------------------------ provider differences
async def test_openai_structured_output_then_fallback_when_unsupported():
    bad = openai.BadRequestError("response_format json_schema is not supported", response=http_response(400), body={"param": "response_format"})
    client = FakeOpenAI([bad, bad, chat_response(valid_extraction_json())])
    e = fast(OpenAIChatEngine("m", client=client, api_key="k"))
    r = await e.generate("s", "u", LLMExtraction, S)
    modes = [c.get("response_format", {}).get("type") for c in client.calls]
    assert modes == ["json_schema", "json_object", None] and r.structured_mode == "none"
    assert client.calls[0]["response_format"]["json_schema"]["strict"] is True


async def test_openai_drops_rejected_temperature_and_swaps_token_param():
    t = openai.BadRequestError("Unsupported value: 'temperature' does not support 0.0", response=http_response(400), body={"param": "temperature"})
    m = openai.BadRequestError("Unsupported parameter: 'max_completion_tokens'. Use 'max_tokens' instead.", response=http_response(400), body={"param": "max_completion_tokens"})
    client = FakeOpenAI([t, m, chat_response("{}")])
    r = await fast(OpenAIChatEngine("m", client=client, api_key="k")).generate("s", "u", None, S)
    assert r.dropped_params == ["temperature"] and "temperature" not in client.calls[-1] and "max_tokens" in client.calls[-1]


async def test_vllm_sends_template_kwargs_and_uses_max_tokens():
    client = FakeOpenAI([chat_response("{}")])
    e = VLLMEngine("m", client=client, base_url="http://x/v1", chat_template_kwargs={"enable_thinking": False})
    await e.generate("s", "u", None, S)
    assert client.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}} and "max_tokens" in client.calls[0]


async def test_anthropic_uses_output_config_and_normalises_response():
    client = FakeAnthropic([anthropic_response("{}")])
    r = await fast(AnthropicEngine("m", client=client)).generate("sys", "u", LLMExtraction, S)
    call = client.calls[0]
    assert call["system"] == "sys" and call["extra_body"]["output_config"]["format"]["type"] == "json_schema" and "tool_choice" not in call
    assert r.content == "{}" and r.engine == "anthropic" and r.finish_reason == "stop" and r.usage == {"prompt_tokens": 5, "completion_tokens": 9}


async def test_anthropic_drops_rejected_temperature_and_unsupported_structured_output():
    t = anthropic.BadRequestError("`temperature` may only be set to 1", response=http_response(400), body=None)
    f = anthropic.BadRequestError("output_config.format is not supported for this model", response=http_response(400), body=None)
    client = FakeAnthropic([t, f, anthropic_response("{}", stop="max_tokens")])
    r = await fast(AnthropicEngine("m", client=client)).generate("s", "u", LLMExtraction, S)
    assert r.dropped_params == ["temperature"] and r.structured_mode == "none" and r.finish_reason == "length"
    assert "temperature" not in client.calls[-1] and "extra_body" not in client.calls[-1]


async def test_non_droppable_bad_request_is_raised():
    client = FakeAnthropic([anthropic.BadRequestError("prompt is too long", response=http_response(400), body=None)])
    with pytest.raises(BadRequest, match="too long"):
        await fast(AnthropicEngine("m", client=client)).generate("s", "u", None, S)
