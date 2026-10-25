"""Thin LLM clients. The agent only needs: system prompt + chat history in, text out."""
import json
import os
import time

import requests

from tools import redact


class LLMError(Exception):
    pass


class ConfigError(Exception):
    pass


RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _post(url, headers, payload, timeout=120, attempts=3):
    delay = 2.0
    last = "unknown error"
    for i in range(attempts):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = f"network error ({type(exc).__name__})"
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    last = "API returned a body that is not JSON"
            elif resp.status_code in RETRY_STATUS:
                last = f"HTTP {resp.status_code}"
            else:
                raise LLMError(f"API error HTTP {resp.status_code}: {redact(resp.text[:300])}")
        if i < attempts - 1:
            time.sleep(delay)
            delay *= 2
    raise LLMError(f"LLM request failed after {attempts} attempts: {last}")


class LLMClient:
    def complete(self, system, messages):
        raise NotImplementedError


class AnthropicClient(LLMClient):
    def __init__(self, api_key, model, base_url=None, temperature=None, max_tokens=4096):
        self.api_key = api_key
        self.model = model
        self.url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        self.temperature = temperature
        self.max_tokens = max_tokens

    def complete(self, system, messages):
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        payload["disable_tool_validation"] = True
        payload["reasoning_effort"] = "low"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        data = _post(self.url, headers, payload)
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise LLMError("model returned no text")
        return text


class OpenAIClient(LLMClient):
    def __init__(self, api_key, model, base_url=None, temperature=None):
        self.api_key = api_key
        self.model = model
        self.url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self.temperature = temperature

    def complete(self, system, messages):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        data = _post(self.url, headers, payload)
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected response shape: {json.dumps(data)[:200]}")
        if not text:
            raise LLMError("model returned no text")
        return text


DEFAULT_MODELS = {"anthropic": "claude-sonnet-4-5", "openai": "gpt-4o"}


def client_from_env(env=None):
    env = os.environ if env is None else env
    provider = (env.get("LLM_PROVIDER") or "anthropic").strip().lower()
    if provider not in DEFAULT_MODELS:
        raise ConfigError(f"LLM_PROVIDER must be 'anthropic' or 'openai', got {provider!r}")
    fallback_key = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    key = (env.get("LLM_API_KEY") or env.get(fallback_key) or "").strip()
    if not key:
        raise ConfigError(f"no API key found: set LLM_API_KEY (or {fallback_key}) in the environment or .env")
    model = (env.get("LLM_MODEL") or "").strip() or DEFAULT_MODELS[provider]
    base_url = (env.get("LLM_BASE_URL") or "").strip() or None
    temp_raw = (env.get("LLM_TEMPERATURE") or "").strip()
    try:
        temperature = float(temp_raw) if temp_raw else None
    except ValueError:
        raise ConfigError(f"LLM_TEMPERATURE is not a number: {temp_raw!r}")
    cls = AnthropicClient if provider == "anthropic" else OpenAIClient
    return cls(key, model, base_url=base_url, temperature=temperature)
