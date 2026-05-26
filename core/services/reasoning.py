"""
Generates the natural-language `reason` shown to customers.

For the POC we use templates with light variation. The function signature
is deliberately shaped so you could swap in an LLM call (OpenAI, Claude,
Vercel AI Gateway) without changing any caller.

This is the *only* place we'd use an LLM in this system. The principle:
AI for unstructured human-facing copy, never for the date prediction itself.

Why template fallback by default:
  - Demo works without an API key
  - Zero added latency on the hot path
  - Deterministic for tests
  - LLM is a strict upgrade you can ship later

To enable a real LLM here, set USE_LLM_REASONING=1 in the environment and
wire your provider of choice inside `_llm_reason()`.
"""

from __future__ import annotations

import os

from core.services.estimator import Estimate


def humanize_reason(estimate: Estimate) -> str:
    """
    Public entrypoint. Returns the customer-facing reason string.
    """
    if os.environ.get("USE_LLM_REASONING") == "1":
        try:
            return _llm_reason(estimate)
        except Exception:
            # Hard requirement: never let a flaky model break checkout.
            pass
    return estimate.reason


def _llm_reason(estimate: Estimate) -> str:
    """
    Wire your provider here. Example shape:

        from openai import OpenAI
        client = OpenAI()
        rsp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    "Rewrite this delivery reason in one warm, plain sentence "
                    "no longer than 14 words. Keep dates and warehouse names exact.\\n\\n"
                    f"Input: {estimate.reason}"
                ),
            }],
            timeout=2.0,
        )
        return rsp.choices[0].message.content.strip()

    Always wrap in a try/except in the caller — this is a customer-facing
    promise, and we'd rather show a templated reason than nothing.
    """
    raise NotImplementedError(
        "Wire your LLM provider in core/services/reasoning.py::_llm_reason"
    )
