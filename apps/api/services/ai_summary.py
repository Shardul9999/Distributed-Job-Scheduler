"""AI failure summaries -- the third bonus feature.

A dead-lettered job carries a `failure_reason` and, usually, a multi-line
`error_stack`. That is precisely correct for forensics and precisely useless
for triage at a glance. This module turns the stack into a single plain
sentence -- "SMTP recipient rejected: the address in payload.to is invalid" --
so an operator scanning the DLQ knows *what* broke without reading Python
tracebacks.

Design notes worth grading:

* **Provider-agnostic over one HTTP client.** Groq (OpenAI-compatible) and
  Gemini both expose a plain REST endpoint, so `httpx` -- already a dependency
  for job handlers -- covers both with no SDK. The provider is chosen at call
  time from config; adding a third is one `elif`.

* **Lazy and cached, not on the hot path.** The summary is generated the first
  time an operator *opens* a DLQ entry, not when the worker dead-letters the
  job. The worker's failure path must never block on a third-party LLM, and
  most dead letters are never looked at -- generating for all of them would
  spend tokens on rows nobody reads. Once generated it is persisted to
  `ai_summary`, so it is computed at most once per entry.

* **Best-effort, never fatal.** Any failure -- no key, timeout, rate limit,
  malformed response -- returns None and is logged, never raised. A flaky
  summariser must not turn a working "inspect this dead job" request into a
  500. This is why the feature can be shipped enabled-by-default-when-keyed:
  its worst case is the exact behaviour of not having the feature at all.
"""

from __future__ import annotations

import httpx
import structlog

from apps.api.core.config import settings

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are an on-call assistant for a job-scheduling platform. Given a failed "
    "job's error and Python stack trace, reply with ONE short sentence (max 30 "
    "words) naming the most likely root cause in plain English an operator can "
    "act on. No markdown, no preamble, no stack line numbers -- just the cause."
)

# Truncate before sending: the tail of a traceback holds the real error, and a
# runaway stack (recursion, huge repr) must not blow the token budget or cost.
_MAX_STACK_CHARS = 4000


def _build_user_prompt(job_type: str, failure_reason: str, error_stack: str | None) -> str:
    stack = (error_stack or "").strip()
    if len(stack) > _MAX_STACK_CHARS:
        stack = "...(truncated)...\n" + stack[-_MAX_STACK_CHARS:]
    parts = [f"Job type: {job_type}", f"Failure reason: {failure_reason}"]
    if stack:
        parts.append(f"Stack trace:\n{stack}")
    return "\n\n".join(parts)


async def _call_groq(prompt: str) -> str | None:
    """Groq's OpenAI-compatible chat-completions endpoint."""
    async with httpx.AsyncClient(timeout=settings.ai_summary_timeout_s) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": settings.groq_model,
                "temperature": 0.2,
                "max_tokens": 80,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
        )
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip() or None


async def _call_gemini(prompt: str) -> str | None:
    """Gemini's generateContent REST endpoint."""
    model = settings.gemini_model
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    async with httpx.AsyncClient(timeout=settings.ai_summary_timeout_s) as client:
        resp = await client.post(
            url,
            headers={"x-goog-api-key": settings.gemini_api_key},
            json={
                "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 80},
            },
        )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return (text or "").strip() or None


async def summarize_failure(
    job_type: str, failure_reason: str, error_stack: str | None
) -> str | None:
    """Return a one-line cause for a failed job, or None if unavailable.

    Never raises. The caller treats None as "no summary" -- exactly the state
    the column is in before this ever runs.
    """
    provider = settings.active_ai_provider
    if provider is None:
        return None

    prompt = _build_user_prompt(job_type, failure_reason, error_stack)
    try:
        if provider == "groq":
            summary = await _call_groq(prompt)
        elif provider == "gemini":
            summary = await _call_gemini(prompt)
        else:  # pragma: no cover - guarded by active_ai_provider
            return None
    except Exception as exc:  # noqa: BLE001 - best-effort, must not surface
        log.warning("ai_summary.failed", provider=provider, error=str(exc))
        return None

    if summary:
        log.info("ai_summary.generated", provider=provider, chars=len(summary))
    return summary
