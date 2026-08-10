"""Small immutable types shared across LLM provider implementations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    max_output_tokens: int
    json_output: bool


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    finish_reason: str | None
    usage: LLMUsage | None
