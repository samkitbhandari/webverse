"""LLM provider abstraction.

Principle 2 of the design brief: *LLMs propose, algorithms reason.* Everything
in this module is proposal machinery -- it turns prose into candidate
structure and never decides what is true. Truth is settled downstream by the
contradiction, temporal and impact engines, which are deterministic.

Three interchangeable backends sit behind one interface:

* ``OpenAIProvider``    - structured JSON output
* ``AnthropicProvider`` - structured JSON output
* ``OfflineProvider``   - no network at all

The offline path is not a stub. A demo that dies without an API key is a demo
that dies on stage, so the rule-based extractor in
:mod:`backend.semantic.rules` is a first-class citizen and the system reports
honestly which one produced a given piece of knowledge (see
``Provenance.extractor``).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from backend.core.config import settings

log = logging.getLogger("nexus.llm")


class LLMProvider(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema_hint: str) -> dict[str, Any]:
        """Return a parsed JSON object, or {} if the provider cannot answer."""

    def complete_text(self, system: str, user: str, max_tokens: int = 600) -> str:
        ...


def _extract_json(raw: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response.

    Models wrap JSON in prose or fences often enough that this is worth doing
    properly rather than trusting ``json.loads`` on the whole string.
    """
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    start = None
    log.warning("could not parse JSON from model response (%d chars)", len(raw))
    return {}


class OfflineProvider:
    """No network. Signals callers to fall back to deterministic extraction."""

    name = "offline"

    def complete_json(self, system: str, user: str, schema_hint: str) -> dict[str, Any]:
        return {}

    def complete_text(self, system: str, user: str, max_tokens: int = 600) -> str:
        return ""


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def complete_json(self, system: str, user: str, schema_hint: str) -> dict[str, Any]:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                temperature=0.1,
                messages=[
                    {"role": "system", "content": f"{system}\n\n{schema_hint}"},
                    {"role": "user", "content": user},
                ],
            )
            return _extract_json(resp.choices[0].message.content or "")
        except Exception as exc:  # network, quota, model errors
            log.warning("openai extraction failed: %s", exc)
            return {}

    def complete_text(self, system: str, user: str, max_tokens: int = 600) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0.2,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            log.warning("openai completion failed: %s", exc)
            return ""


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete_json(self, system: str, user: str, schema_hint: str) -> dict[str, Any]:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.1,
                system=f"{system}\n\n{schema_hint}\n"
                       "Respond with a single JSON object and nothing else.",
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            )
            return _extract_json(text)
        except Exception as exc:
            log.warning("anthropic extraction failed: %s", exc)
            return {}

    def complete_text(self, system: str, user: str, max_tokens: int = 600) -> str:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0.2,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            )
        except Exception as exc:
            log.warning("anthropic completion failed: %s", exc)
            return ""


_provider: LLMProvider | None = None


def get_llm(force: str | None = None) -> LLMProvider:
    """Resolve the configured provider, falling back to offline."""
    global _provider
    if _provider is not None and force is None:
        return _provider

    choice = force or settings.llm_provider
    try:
        if choice in ("auto", "openai") and settings.openai_api_key:
            _provider = OpenAIProvider(settings.openai_api_key, settings.openai_model)
        elif choice in ("auto", "anthropic") and settings.anthropic_api_key:
            _provider = AnthropicProvider(
                settings.anthropic_api_key, settings.anthropic_model
            )
        else:
            _provider = OfflineProvider()
    except Exception as exc:  # missing SDK, bad key format
        log.warning("provider init failed (%s); using offline extraction", exc)
        _provider = OfflineProvider()

    log.info("semantic compiler provider: %s", _provider.name)
    return _provider


def reset_llm() -> None:
    global _provider
    _provider = None
