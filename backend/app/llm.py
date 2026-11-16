"""One interface over the supported AI providers.

`complete()` returns text, `complete_json()` returns parsed JSON. The provider and model are the
signed-in person's own choice. Their own key is used if they saved one; otherwise NVIDIA falls back
to the key the server provides for everyone. Other providers need the person's own key.

Work runs on behalf of one person (`as_user`), so a job always uses that person's settings and keys.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import re
import time
from typing import Any, Iterator

import httpx

from . import config, db, keys

_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_user", default=None)


@contextlib.contextmanager
def as_user(user_id: str | None) -> Iterator[None]:
    """Run AI calls for this person: their provider, model and keys."""
    token = _user.set(user_id)
    try:
        yield
    finally:
        _user.reset(token)

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


def _provider() -> tuple[str, str, str]:
    user = _user.get()
    ai = db.settings_for(user)["ai"]
    provider = ai["provider"]
    if provider not in config.PROVIDER_SECRET:
        raise LLMError(f"Unknown AI provider '{provider}'. Pick one in Settings.")
    secret_name = config.PROVIDER_SECRET[provider]
    try:
        key = keys.effective(user, secret_name) if secret_name else ""
    except keys.KeysError as exc:
        raise LLMError(str(exc)) from None
    if secret_name and not key:
        raise LLMError(f"No API key set for {provider}. Add your own in Settings → AI engine.")
    return provider, ai["models"][provider], key


def _request(provider: str, model: str, key: str, system: str, user: str,
             max_tokens: int, temperature: float) -> str:
    if provider == "nvidia":
        resp = httpx.post(
            NVIDIA_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=240,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"].get("content") or ""
    if provider == "anthropic":
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=240,
        )
        resp.raise_for_status()
        return "".join(b.get("text", "") for b in resp.json()["content"] if b.get("type") == "text")
    if provider == "gemini":
        resp = httpx.post(
            GEMINI_URL.format(model=model),
            headers={"x-goog-api-key": key},
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
            },
            timeout=240,
        )
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    resp = httpx.post(
        OLLAMA_URL,
        json={
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": temperature},
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def complete(system: str, user: str, *, max_tokens: int = 6000,
             temperature: float = 0.4, attempts: int = 4) -> str:
    provider, model, key = _provider()
    last = "unknown error"
    for attempt in range(attempts):
        try:
            text = _request(provider, model, key, system, user, max_tokens, temperature)
            if text.strip():
                return text
            last = "the model returned an empty answer"
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise LLMError(f"{provider} rejected the API key. Replace it in Settings.") from None
            if status == 404:
                raise LLMError(f"{provider} does not serve the model '{model}'. "
                               "Change the model in Settings.") from None
            last = f"{provider} returned HTTP {status}"
            if status not in RETRY_STATUS:
                break
        except httpx.HTTPError as exc:
            last = f"could not reach {provider} ({type(exc).__name__})"
        time.sleep(min(2 ** attempt * 2, 20))
    raise LLMError(f"AI request failed: {last}.")


def parse_json(text: str) -> Any:
    """Parse JSON from a model answer, tolerating code fences and stray prose."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON found in the model answer")
    start = min(starts)
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    return json.loads(cleaned[start:end + 1])


def complete_json(system: str, user: str, **kwargs: Any) -> Any:
    system = system + "\n\nAnswer with valid JSON only. No prose, no code fences."
    error: Exception | None = None
    for _ in range(2):
        try:
            return parse_json(complete(system, user, **kwargs))
        except ValueError as exc:
            error = exc
    raise LLMError(f"The AI answer was not valid JSON ({error}).")


def ping() -> dict[str, Any]:
    provider, model, _ = _provider()
    started = time.time()
    data = complete_json("You are a health check.", 'Return {"ok": true}.', max_tokens=300)
    return {"ok": bool(data.get("ok")), "provider": provider, "model": model,
            "seconds": round(time.time() - started, 2)}
