"""Unit tests for vision_providers.py — the provider-agnostic judgment
client. SDK calls are mocked throughout (no real OpenAI/Grok/Llama-host/
Anthropic account is exercised here, same caveat as
test_notifications.py in the rules-engine service)."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from vision_providers import (  # noqa: E402
    AnthropicProvider, OpenAICompatibleProvider, SceneJudgment,
    _parse_judgment, build_vision_provider,
)


# ── _parse_judgment ──────────────────────────────────────────────────────

def test_parse_judgment_positive():
    text = json.dumps({
        "needs_task": True, "task_type": "restock_concession",
        "task_name": "Restock the snack display", "message": "Rack looks picked-over.",
        "confidence": 0.82,
    })
    j = _parse_judgment(text)
    assert j.needs_task is True
    assert j.task_type == "restock_concession"
    assert j.task_name == "Restock the snack display"
    assert j.confidence == 0.82


def test_parse_judgment_negative():
    text = json.dumps({"needs_task": False, "task_type": None, "task_name": None,
                        "message": "Looks normal.", "confidence": 0.95})
    j = _parse_judgment(text)
    assert j.needs_task is False
    assert j.task_type is None
    assert j.task_name is None


def test_parse_judgment_strips_surrounding_prose():
    text = 'Sure, here is my analysis:\n```json\n{"needs_task": false, "confidence": 0.5}\n```\nHope that helps!'
    j = _parse_judgment(text)
    assert j is not None
    assert j.needs_task is False


def test_parse_judgment_rejects_unknown_task_type():
    """An unrecognized task_type is treated as "not actionable" rather
    than silently passed through — the rules engine would still accept
    it, but this guards against a model hallucinating a type nobody
    calibrated a threshold for."""
    text = json.dumps({"needs_task": True, "task_type": "reorganize_warehouse",
                        "task_name": "x", "confidence": 0.9})
    j = _parse_judgment(text)
    assert j.needs_task is False
    assert j.task_type is None


def test_parse_judgment_malformed_json_returns_none():
    assert _parse_judgment("not json at all") is None


def test_parse_judgment_empty_string_returns_none():
    assert _parse_judgment("") is None
    assert _parse_judgment(None) is None


def test_parse_judgment_clamps_confidence_to_valid_range():
    text = json.dumps({"needs_task": True, "task_type": "clean_door", "task_name": "x", "confidence": 5.0})
    j = _parse_judgment(text)
    assert j.confidence == 1.0

    text2 = json.dumps({"needs_task": True, "task_type": "clean_door", "task_name": "x", "confidence": -3.0})
    j2 = _parse_judgment(text2)
    assert j2.confidence == 0.0


def test_parse_judgment_bad_confidence_type_defaults_to_zero():
    text = json.dumps({"needs_task": False, "confidence": "very confident"})
    j = _parse_judgment(text)
    assert j.confidence == 0.0


# ── OpenAICompatibleProvider (covers openai/grok/llama — same class) ────

def _fake_openai_module(response_text):
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content=response_text))]
    fake_client_instance = MagicMock()
    fake_client_instance.chat.completions.create.return_value = fake_response
    fake_openai_cls = MagicMock(return_value=fake_client_instance)
    fake_module = MagicMock()
    fake_module.OpenAI = fake_openai_cls
    return fake_module, fake_client_instance


def test_openai_compatible_provider_judges_from_mocked_response(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0fakejpegbytes")
    response_text = json.dumps({"needs_task": True, "task_type": "clean_door",
                                 "task_name": "Clean the entrance", "confidence": 0.7})
    fake_module, fake_client = _fake_openai_module(response_text)

    with patch.dict(sys.modules, {"openai": fake_module}):
        provider = OpenAICompatibleProvider(api_key="sk-fake", model="gpt-4o-mini")
        judgment = provider.judge(frame)

    assert judgment.needs_task is True
    assert judgment.task_type == "clean_door"
    # confirms the image was actually sent as an image_url content block
    call_kwargs = fake_client.chat.completions.create.call_args.kwargs
    content = call_kwargs["messages"][0]["content"]
    assert any(block["type"] == "image_url" for block in content)
    assert call_kwargs["model"] == "gpt-4o-mini"


def test_openai_compatible_provider_passes_base_url_for_grok_llama():
    fake_module, _ = _fake_openai_module(json.dumps({"needs_task": False}))
    with patch.dict(sys.modules, {"openai": fake_module}):
        OpenAICompatibleProvider(api_key="sk-fake", model="grok-2-vision", base_url="https://api.x.ai/v1")
    fake_module.OpenAI.assert_called_once_with(api_key="sk-fake", base_url="https://api.x.ai/v1")


def test_openai_compatible_provider_returns_none_on_missing_frame(tmp_path):
    fake_module, _ = _fake_openai_module("{}")
    with patch.dict(sys.modules, {"openai": fake_module}):
        provider = OpenAICompatibleProvider(api_key="sk-fake", model="gpt-4o-mini")
        result = provider.judge(tmp_path / "does_not_exist.jpg")
    assert result is None


def test_openai_compatible_provider_never_raises_on_api_error(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"fake")
    fake_client_instance = MagicMock()
    fake_client_instance.chat.completions.create.side_effect = Exception("rate limited")
    fake_module = MagicMock()
    fake_module.OpenAI = MagicMock(return_value=fake_client_instance)

    with patch.dict(sys.modules, {"openai": fake_module}):
        provider = OpenAICompatibleProvider(api_key="sk-fake", model="gpt-4o-mini")
        result = provider.judge(frame)  # must not raise
    assert result is None


# ── AnthropicProvider ─────────────────────────────────────────────────────

def test_anthropic_provider_judges_from_mocked_response(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0fakejpegbytes")
    response_text = json.dumps({"needs_task": True, "task_type": "restroom_check",
                                 "task_name": "Check the restroom", "confidence": 0.6})
    fake_text_block = MagicMock(type="text", text=response_text)
    fake_response = MagicMock(content=[fake_text_block])
    fake_client_instance = MagicMock()
    fake_client_instance.messages.create.return_value = fake_response
    fake_module = MagicMock()
    fake_module.Anthropic = MagicMock(return_value=fake_client_instance)

    with patch.dict(sys.modules, {"anthropic": fake_module}):
        provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-haiku-4-5")
        judgment = provider.judge(frame)

    assert judgment.needs_task is True
    assert judgment.task_type == "restroom_check"
    call_kwargs = fake_client_instance.messages.create.call_args.kwargs
    content = call_kwargs["messages"][0]["content"]
    assert any(block["type"] == "image" for block in content)


def test_anthropic_provider_never_raises_on_api_error(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"fake")
    fake_client_instance = MagicMock()
    fake_client_instance.messages.create.side_effect = Exception("overloaded")
    fake_module = MagicMock()
    fake_module.Anthropic = MagicMock(return_value=fake_client_instance)

    with patch.dict(sys.modules, {"anthropic": fake_module}):
        provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-haiku-4-5")
        result = provider.judge(frame)
    assert result is None


# ── build_vision_provider dispatch ────────────────────────────────────────

def test_build_vision_provider_none_returns_none():
    assert build_vision_provider("none", "key", "model") is None
    assert build_vision_provider("", "key", "model") is None


def test_build_vision_provider_missing_api_key_returns_none():
    assert build_vision_provider("openai", "", "gpt-4o-mini") is None


def test_build_vision_provider_openai():
    fake_module, _ = _fake_openai_module("{}")
    with patch.dict(sys.modules, {"openai": fake_module}):
        provider = build_vision_provider("openai", "sk-fake", "gpt-4o-mini")
    assert isinstance(provider, OpenAICompatibleProvider)


def test_build_vision_provider_grok_uses_xai_base_url():
    fake_module, _ = _fake_openai_module("{}")
    with patch.dict(sys.modules, {"openai": fake_module}):
        build_vision_provider("grok", "sk-fake", "grok-2-vision")
    fake_module.OpenAI.assert_called_once_with(api_key="sk-fake", base_url="https://api.x.ai/v1")


def test_build_vision_provider_llama_requires_base_url():
    assert build_vision_provider("llama", "sk-fake", "llama-3.2-11b-vision") is None


def test_build_vision_provider_llama_with_base_url():
    fake_module, _ = _fake_openai_module("{}")
    with patch.dict(sys.modules, {"openai": fake_module}):
        provider = build_vision_provider(
            "llama", "sk-fake", "llama-3.2-11b-vision", base_url="https://api.together.xyz/v1")
    assert isinstance(provider, OpenAICompatibleProvider)
    fake_module.OpenAI.assert_called_once_with(api_key="sk-fake", base_url="https://api.together.xyz/v1")


def test_build_vision_provider_claude():
    fake_module = MagicMock()
    fake_module.Anthropic = MagicMock(return_value=MagicMock())
    with patch.dict(sys.modules, {"anthropic": fake_module}):
        provider = build_vision_provider("claude", "sk-ant-fake", "claude-haiku-4-5")
    assert isinstance(provider, AnthropicProvider)


def test_build_vision_provider_anthropic_alias():
    fake_module = MagicMock()
    fake_module.Anthropic = MagicMock(return_value=MagicMock())
    with patch.dict(sys.modules, {"anthropic": fake_module}):
        provider = build_vision_provider("anthropic", "sk-ant-fake", "claude-haiku-4-5")
    assert isinstance(provider, AnthropicProvider)


def test_build_vision_provider_unrecognized_returns_none():
    assert build_vision_provider("carrier-pigeon", "key", "model") is None


def test_build_vision_provider_never_raises_on_init_failure():
    fake_module = MagicMock()
    fake_module.OpenAI = MagicMock(side_effect=Exception("bad key format"))
    with patch.dict(sys.modules, {"openai": fake_module}):
        provider = build_vision_provider("openai", "sk-fake", "gpt-4o-mini")
    assert provider is None
