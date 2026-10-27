"""Thin wrapper around the Anthropic SDK: per-step models, JSON calls, web research calls, cost tracking."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from dotenv import load_dotenv

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONTINUATIONS = 5
WEB_SEARCH_USD = 0.01

# USD per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


class ClaudeError(RuntimeError):
    pass


def _is_haiku(model: str) -> bool:
    return "haiku" in model


def web_tools(model: str, max_searches: int, max_fetches: int = 0) -> list[dict]:
    # Haiku takes the basic tool versions; Sonnet/Opus get the dynamic-filtering ones.
    search, fetch = (("web_search_20250305", "web_fetch_20250910") if _is_haiku(model)
                     else ("web_search_20260209", "web_fetch_20260209"))
    tools = [{"type": search, "name": "web_search", "max_uses": max_searches}]
    if max_fetches:
        tools.append({"type": fetch, "name": "web_fetch", "max_uses": max_fetches})
    return tools


@dataclass
class Usage:
    calls: int = 0
    web_searches: int = 0
    cost: float = 0.0
    by_step: dict = field(default_factory=dict)  # step -> {"calls", "cost"}

    def add(self, response, step: str, model: str) -> None:
        u = response.usage
        p_in, p_out, p_read = PRICES.get(model, PRICES["claude-opus-5-5"])
        searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
        cost = (
            (u.input_tokens or 0) * p_in
            + (getattr(u, "cache_creation_input_tokens", 0) or 0) * p_in * 1.25
            + (getattr(u, "cache_read_input_tokens", 0) or 0) * p_read
            + (u.output_tokens or 0) * p_out
        ) / 1_000_000 + searches * WEB_SEARCH_USD
        self.calls += 1
        self.web_searches += searches
        self.cost += cost
        s = self.by_step.setdefault(step, {"calls": 0, "cost": 0.0})
        s["calls"] += 1
        s["cost"] += cost

    def summary(self) -> str:
        steps = ", ".join(f"{k} {v['calls']}x ${v['cost']:.3f}" for k, v in self.by_step.items())
        return (f"{self.calls} Claude calls, {self.web_searches} web searches, est. ${self.cost:.2f}"
                + (f"  ({steps})" if steps else ""))


class Claude:
    def __init__(self, models: dict[str, str]):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        key = os.environ.get("CLAUDE_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("No API key: set CLAUDE_KEY in .env")
        self.client = anthropic.Anthropic(api_key=key, max_retries=4)
        self.models = models
        self.usage = Usage()

    def model_for(self, step: str) -> str:
        return self.models[step]

    def _create(self, step: str, **kwargs):
        model = self.model_for(step)
        extra = {} if _is_haiku(model) else {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        # Streamed so long research turns don't hit HTTP timeouts.
        with self.client.beta.messages.stream(
            model=model, thinking={"type": "adaptive"}, **extra, **kwargs
        ) as stream:
            response = stream.get_final_message()
        self.usage.add(response, step, model)
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise ClaudeError(f"{step}: request declined (category={category})")
        if response.stop_reason == "max_tokens":
            raise ClaudeError(f"{step}: response hit max_tokens")
        return response

    def _run(self, step: str, system: str, prompt: str, max_tokens: int, **kwargs) -> list:
        """Run one turn, resuming if a server-side tool loop pauses. Returns all content blocks."""
        user_turn = {"role": "user", "content": prompt}
        content = []
        for _ in range(MAX_CONTINUATIONS + 1):
            messages = [user_turn]
            if content:
                # Server-side tool loop paused; resending the partial turn resumes it.
                messages.append({"role": "assistant", "content": content})
            response = self._create(
                step,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                **kwargs,
            )
            content.extend(response.content)
            if response.stop_reason != "pause_turn":
                return content
        raise ClaudeError(f"{step}: still paused after {MAX_CONTINUATIONS} continuations")

    def json_call(self, step: str, system: str, prompt: str, schema: dict, effort: str = "low",
                  max_tokens: int = 16000, tools: list | None = None) -> dict:
        """Reply is guaranteed to match `schema`. Optional server tools (e.g. web search)."""
        content = self._run(
            step, system, prompt, max_tokens,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            **({"tools": tools} if tools else {}),
        )
        texts = [b.text for b in content if b.type == "text"]
        if not texts:
            raise ClaudeError(f"{step}: no text block in response")
        return json.loads(texts[-1])

    def research_call(self, step: str, system: str, prompt: str, max_searches: int,
                      max_fetches: int, effort: str = "low") -> str:
        """Web-search-enabled call; returns Claude's final written notes."""
        content = self._run(
            step, system, prompt, 32000,
            tools=web_tools(self.model_for(step), max_searches, max_fetches),
            output_config={"effort": effort},
        )
        return "\n".join(b.text for b in content if b.type == "text").strip()
