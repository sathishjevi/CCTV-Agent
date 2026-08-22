"""Provider-agnostic vision-judgment client for the scene-condition skill.

Explicitly NOT locked to one AI vendor — a client may already have (or
prefer to pay for) OpenAI, xAI Grok, a hosted Llama vision model, or
Claude, and this skill has to run against whichever they bring. Three
providers, two implementations:

  - OpenAICompatibleProvider handles OpenAI, Grok, AND any Llama host
    (Together/Fireworks/Groq/Deepinfra/etc.) — all three speak the same
    chat-completions-with-image-content API shape; only the base_url and
    model name differ. One implementation, three provider names.
  - AnthropicProvider handles Claude — a different SDK/request shape.

Adding a fourth provider that happens to be OpenAI-compatible (most new
hosted vision APIs are) means adding one line to build_vision_provider()
below, not a new class.

Same honest-failure-mode discipline as notifications.py elsewhere in
this codebase: never raises on a bad/missing response, a malformed
image, or an unrecognized provider name — logs and returns None (no
task suggested) instead, since a false negative here just means one
missed check, while an unhandled exception would crash the whole
polling loop over one bad frame.

Caveat, stated plainly rather than glossed over: this has been tested
against mocked SDK clients (see tests/test_vision_providers.py) — no
live OpenAI/Grok/Llama-host/Anthropic account has been exercised in
this sandbox. Same caveat notifications.py's Twilio/FCM code carries.
"""

import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Keys must match task_type_thresholds.json in the rules-engine service —
# an unrecognized task_type would still create a task fine (falls back to
# "_default" threshold there), but keeping this list in sync means the
# model is always asked to classify into a type the effort engine already
# has a calibrated expectation for.
KNOWN_TASK_TYPES = ["clean_door", "restock_concession", "restroom_check", "lobby_sweep"]

PROMPT = f"""You are reviewing a single still frame from a retail/venue security camera. Judge whether the scene shows a condition that needs staff attention right now — a messy or depleted display, a spill, a blocked walkway, an obviously dirty surface. Do NOT flag normal customer activity, people browsing, checkout lines, or anything that isn't a physical condition needing cleanup/restocking.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{"needs_task": true or false, "task_type": one of {KNOWN_TASK_TYPES} or null, "task_name": a short human-readable task title (e.g. "Restock the snack display") or null, "message": one sentence explaining what you saw, "confidence": a number from 0 to 1}}

If nothing needs attention, respond with {{"needs_task": false, "task_type": null, "task_name": null, "message": "Scene looks normal.", "confidence": <your confidence in that judgment>}}."""


def _log(msg: str):
    print(f"[floorwatch-scene-condition:vision] {msg}", file=sys.stderr, flush=True)


@dataclass
class SceneJudgment:
    needs_task: bool
    task_type: Optional[str] = None
    task_name: Optional[str] = None
    message: Optional[str] = None
    confidence: float = 0.0


def _image_data_url(frame_path: Path) -> Optional[str]:
    try:
        raw = frame_path.read_bytes()
    except OSError as e:
        _log(f"could not read frame {frame_path}: {e}")
        return None
    suffix = frame_path.suffix.lower().lstrip(".") or "jpeg"
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    return f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _parse_judgment(raw_text: str) -> Optional[SceneJudgment]:
    """Models occasionally wrap JSON in prose or code fences despite
    instructions — extract the first {...} block rather than requiring
    an exact-match response, but never raise on genuinely malformed
    output."""
    if not raw_text:
        return None
    text = raw_text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        _log(f"no JSON object found in model response: {text[:200]!r}")
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        _log(f"could not parse model response as JSON: {e} — {text[:200]!r}")
        return None

    needs_task = bool(data.get("needs_task"))
    task_type = data.get("task_type")
    if task_type not in KNOWN_TASK_TYPES:
        task_type = None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return SceneJudgment(
        needs_task=needs_task and task_type is not None,  # a "yes" with no classifiable type isn't actionable
        task_type=task_type if needs_task else None,
        task_name=(data.get("task_name") or None) if needs_task else None,
        message=data.get("message") or None,
        confidence=confidence,
    )


class OpenAICompatibleProvider:
    """OpenAI, xAI Grok, and any OpenAI-compatible hosted Llama vision
    endpoint (Together/Fireworks/Groq/Deepinfra/...) — same request
    shape, only base_url + model differ."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        from openai import OpenAI  # imported lazily — optional dependency unless this provider is used
        self._client = OpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model

    def judge(self, frame_path: Path) -> Optional[SceneJudgment]:
        data_url = _image_data_url(frame_path)
        if data_url is None:
            return None
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
                max_tokens=300,
            )
            text = resp.choices[0].message.content
        except Exception as e:
            _log(f"vision request failed: {e}")
            return None
        return _parse_judgment(text)


class AnthropicProvider:
    """Claude — kept as one of the supported providers, not the only
    one; see this module's docstring for why the default here isn't
    Anthropic-only."""

    def __init__(self, api_key: str, model: str):
        from anthropic import Anthropic  # imported lazily, same rationale as the OpenAI-compatible client
        self._client = Anthropic(api_key=api_key)
        self.model = model

    def judge(self, frame_path: Path) -> Optional[SceneJudgment]:
        try:
            raw = frame_path.read_bytes()
        except OSError as e:
            _log(f"could not read frame {frame_path}: {e}")
            return None
        suffix = frame_path.suffix.lower().lstrip(".") or "jpeg"
        media_type = f"image/{'jpeg' if suffix in ('jpg', 'jpeg') else suffix}"
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": media_type,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }},
                        {"type": "text", "text": PROMPT},
                    ],
                }],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        except Exception as e:
            _log(f"vision request failed: {e}")
            return None
        return _parse_judgment(text)


# provider name -> (class, fixed base_url or None if the caller must supply one)
_OPENAI_COMPATIBLE_BASE_URLS = {
    "openai": None,           # OpenAI SDK default (api.openai.com)
    "grok": "https://api.x.ai/v1",
    "llama": None,            # no single default — Llama hosts vary; base_url is required for this one
}


def build_vision_provider(provider: str, api_key: str, model: str, base_url: Optional[str] = None):
    """provider: "none" | "openai" | "grok" | "llama" | "claude" | "anthropic".
    Never raises — returns None on missing credentials, an unrecognized
    provider name, or an SDK import failure (missing package), so a bad
    deployment config disables scene detection rather than crashing the
    polling loop. Mirrors notifications.py's build_sender() pattern."""
    if not provider or provider == "none":
        return None
    if not api_key:
        _log(f"provider '{provider}' configured but no API key set — scene detection disabled")
        return None

    provider = provider.lower()
    try:
        if provider in _OPENAI_COMPATIBLE_BASE_URLS:
            resolved_base_url = base_url or _OPENAI_COMPATIBLE_BASE_URLS[provider]
            if provider == "llama" and not resolved_base_url:
                _log("provider 'llama' requires base_url (which host is serving the model) — scene detection disabled")
                return None
            return OpenAICompatibleProvider(api_key, model, resolved_base_url)
        if provider in ("claude", "anthropic"):
            return AnthropicProvider(api_key, model)
    except Exception as e:
        _log(f"could not initialize provider '{provider}' ({e}) — scene detection disabled")
        return None

    _log(f"unrecognized provider '{provider}' — scene detection disabled")
    return None
