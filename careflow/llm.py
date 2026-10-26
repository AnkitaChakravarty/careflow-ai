"""One gateway for every model call: budget check -> call -> log real token usage.

Every call records input, cached-input and output tokens and the cost, so the
app can show its own tokenomics (Usage page)."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date

from . import config
from .db import now


class BudgetExceeded(RuntimeError):
    pass


class ModelUnavailable(RuntimeError):
    pass


class ContentFiltered(ModelUnavailable):
    """The provider's safety filter blocked the prompt (e.g. a jailbreak attempt)."""


def cost_usd(inp: int, cached: int, out: int) -> float:
    fresh = max(0, inp - cached)
    return (fresh * config.PRICE_INPUT + cached * config.PRICE_CACHED_INPUT + out * config.PRICE_OUTPUT) / 1_000_000


def tokens_today(conn: sqlite3.Connection, patient_id: int | None = None, user_id: int | None = None) -> int:
    q = "SELECT COALESCE(SUM(input_tokens + output_tokens),0) FROM usage WHERE substr(ts,1,10)=?"
    args: list = [date.today().isoformat()]
    if patient_id is not None:
        q += " AND patient_id=?"
        args.append(patient_id)
    if user_id is not None:
        q += " AND user_id=?"
        args.append(user_id)
    return int(conn.execute(q, args).fetchone()[0])


def check_budget(conn: sqlite3.Connection, patient_id: int | None, user_id: int | None = None) -> None:
    if tokens_today(conn) >= config.DAILY_TOKEN_BUDGET:
        raise BudgetExceeded(f"Daily AI budget of {config.DAILY_TOKEN_BUDGET:,} tokens reached. "
                             "AI features are paused until tomorrow; manual entry still works.")
    if patient_id is not None and tokens_today(conn, patient_id) >= config.PER_PATIENT_DAILY_BUDGET:
        raise BudgetExceeded(f"This patient's daily AI budget ({config.PER_PATIENT_DAILY_BUDGET:,} tokens) is used up.")
    if user_id is not None and tokens_today(conn, user_id=user_id) >= config.USER_DAILY_BUDGET:
        raise BudgetExceeded(f"Your personal daily AI budget ({config.USER_DAILY_BUDGET:,} tokens) is used up.")


def _log(conn, feature, patient_id, inp, cached, out, latency, ok, note="", user_id=None):
    conn.execute(
        "INSERT INTO usage (ts, feature, patient_id, user_id, model, input_tokens, cached_tokens, output_tokens, cost_usd, latency_s, ok, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now(), feature, patient_id, user_id, config.MODEL, inp, cached, out, cost_usd(inp, cached, out), round(latency, 2), int(ok), note),
    )
    conn.commit()


_client = None


def _get_client():
    global _client
    if _client is None:
        key = config.api_key()
        if not key:
            raise ModelUnavailable("PORTKEY_API_KEY not found. Put a .env file in the repo folder or the folder above it.")
        from openai import OpenAI
        _client = OpenAI(api_key=key, base_url=config.BASE_URL, timeout=120, max_retries=1)
    return _client


# Mock replies (CAREFLOW_MOCK=1) so the UI and tests run without a key. Set by each agent module.
MOCK_HANDLERS: dict = {}


def chat(conn: sqlite3.Connection, feature: str, patient_id: int | None, messages: list,
         tools: list | None = None, json_mode: bool = False, max_tokens: int | None = None, user_id: int | None = None):
    """Returns the OpenAI-style message object (has .content and .tool_calls)."""
    check_budget(conn, patient_id, user_id)
    t0 = time.time()
    if config.MOCK:
        msg, usage = MOCK_HANDLERS[feature](messages, tools)
        _log(conn, feature, patient_id, usage[0], usage[1], usage[2], time.time() - t0, True, "mock", user_id)
        return msg
    kwargs = dict(model=config.MODEL, messages=messages, max_completion_tokens=max_tokens or config.MAX_OUTPUT_TOKENS)
    if tools:
        kwargs["tools"] = tools
        # gpt-6-luna on /v1/chat/completions rejects function tools unless reasoning is off.
        kwargs["reasoning_effort"] = "none"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = _get_client().chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        _log(conn, feature, patient_id, 0, 0, 0, time.time() - t0, False, type(exc).__name__, user_id)
        key = config.api_key()
        text = str(exc).replace(key, "<REDACTED>") if key else str(exc)
        if "content_filter" in text or "content management policy" in text:
            raise ContentFiltered("The request was blocked by the model provider's safety filter.") from exc
        raise ModelUnavailable(f"Model call failed ({type(exc).__name__}): {text[:300]}") from exc
    u = resp.usage
    cached = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    _log(conn, feature, patient_id, u.prompt_tokens, cached, u.completion_tokens, time.time() - t0, True, "", user_id)
    return resp.choices[0].message


def parse_json(text: str | None) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model did not return JSON.")
    return json.loads(text[start:end + 1])
