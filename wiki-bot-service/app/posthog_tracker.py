import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from typing import Optional, Dict, Any, List

import httpx
import posthog

logger = logging.getLogger(__name__)

POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY", "")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://eu.i.posthog.com")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "")
CAPTURE_CONTENT = os.getenv("POSTHOG_CAPTURE_CONTENT", "").lower() == "true"

if POSTHOG_API_KEY:
    posthog.api_key = POSTHOG_API_KEY
    posthog.host = POSTHOG_HOST
    # Disable PostHog's own library debug logging
    posthog.debug = False


# ── Privacy layer ───────────────────────────────────────────────────
# Chat IDs are pseudonymised (HMAC), person profiles and GeoIP are off, and every
# property is checked against an allowlist: only numbers/bools with size-like
# names and short enum-like strings under known keys leave the process.
_ID_KEY = (
    os.getenv("POSTHOG_ID_SALT") or os.getenv("TELEGRAM_BOT_TOKEN") or secrets.token_hex(16)
).encode()

SAFE_STR_KEYS = {
    "intent", "cadence", "chat_type", "page_type", "error_type", "retrieval",
    "model", "provider", "label", "instance", "$ai_model", "$ai_provider", "$ai_base_url",
}
# $ai_input / $ai_output_choices are already redacted to sizes unless CAPTURE_CONTENT
PASSTHROUGH_KEYS = {"$ai_input", "$ai_output_choices"}
_NUM_KEY = re.compile(r"(_length|_bytes|_count|_seconds|_size|_tokens|_status|_usd|_latency)$|^has_|^\$ai_")
_SAFE_STR = re.compile(r"^[A-Za-z0-9_./:\- ]{1,80}$")


def _distinct_id(chat_id: Optional[int] = None) -> str:
    if not chat_id:
        return "wiki-bot"
    return hmac.new(_ID_KEY, str(chat_id).encode(), hashlib.sha256).hexdigest()[:16]


def sanitize(props: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for k, v in (props or {}).items():
        if k in PASSTHROUGH_KEYS:
            clean[k] = v
        elif isinstance(v, bool):
            clean[k] = v
        elif isinstance(v, (int, float)) and _NUM_KEY.search(k):
            clean[k] = v
        elif isinstance(v, str) and k in SAFE_STR_KEYS and _SAFE_STR.match(v):
            clean[k] = v
        else:
            logger.debug(f"PostHog property dropped by privacy filter: {k}")
    if INSTANCE_NAME:
        clean["instance"] = INSTANCE_NAME
    clean["$process_person_profile"] = False
    clean["$geoip_disable"] = True
    return clean


def error_props(exc: BaseException) -> Dict[str, Any]:
    """Safe error description: class name and HTTP status only, never the message."""
    props: Dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(exc, httpx.HTTPStatusError):
        props["http_status"] = exc.response.status_code
    return props


def track_ai_generation(
    *,
    distinct_id: Optional[int] = None,
    event_name: str = "$ai_generation",
    model: str,
    provider: str,
    messages: List[Dict[str, str]],
    response: str,
    latency_seconds: float,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
    http_status: int = 200,
    base_url: str = "",
) -> None:
    """Track an LLM generation call with PostHog AI observability."""
    if not POSTHOG_API_KEY:
        return

    properties: Dict[str, Any] = {
        "$ai_model": model,
        "$ai_provider": provider,
        # Prompt/response content (wiki, notes, transcripts) is private; send sizes only
        # unless POSTHOG_CAPTURE_CONTENT=true.
        "$ai_input": messages if CAPTURE_CONTENT else [
            {"role": m["role"], "content": f"[redacted, {len(m['content'])} chars]"} for m in messages
        ],
        "$ai_output_choices": [
            {"role": "assistant", "content": response if CAPTURE_CONTENT else f"[redacted, {len(response)} chars]"}
        ],
        "$ai_latency": round(latency_seconds, 3),
        "$ai_http_status": http_status,
    }
    if base_url:
        properties["$ai_base_url"] = base_url
    if input_tokens is not None:
        properties["$ai_input_tokens"] = input_tokens
    if output_tokens is not None:
        properties["$ai_output_tokens"] = output_tokens
    if total_tokens is not None:
        properties["$ai_total_tokens"] = total_tokens
    if cost_usd is not None:
        properties["$ai_cost_usd"] = round(cost_usd, 6)

    if metadata:
        properties.update(metadata)

    posthog.capture(
        distinct_id=_distinct_id(distinct_id),
        event=event_name,
        properties=sanitize(properties),
    )


def track_event(
    *,
    distinct_id: Optional[int] = None,
    event: str,
    properties: Optional[Dict[str, Any]] = None,
) -> None:
    """Track a custom business event."""
    if not POSTHOG_API_KEY:
        return

    posthog.capture(
        distinct_id=_distinct_id(distinct_id),
        event=event,
        properties=sanitize(properties),
    )


class LLMTracker:
    """Context manager / decorator helper to time and track LLM calls."""

    def __init__(
        self,
        *,
        chat_id: Optional[int] = None,
        model: str = "",
        provider: str = "",
        base_url: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.chat_id = chat_id
        self.model = model
        self.provider = provider
        self.base_url = base_url
        self.metadata = metadata or {}
        self.start_ts: float = 0.0
        self.latency: float = 0.0
        self.http_status: int = 200

    def __enter__(self):
        self.start_ts = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.latency = time.perf_counter() - self.start_ts
        if exc_val:
            self.http_status = 500
            track_event(
                distinct_id=self.chat_id,
                event="llm_error",
                properties={
                    "model": self.model,
                    "provider": self.provider,
                    **error_props(exc_val),
                    "latency": round(self.latency, 3),
                    **self.metadata,
                },
            )
        return False

    def track_success(
        self,
        messages: List[Dict[str, str]],
        response: str,
        usage: Optional[Dict[str, int]] = None,
    ):
        self.latency = time.perf_counter() - self.start_ts
        input_tokens = usage.get("prompt_tokens") if usage else None
        output_tokens = usage.get("completion_tokens") if usage else None
        total_tokens = usage.get("total_tokens") if usage else None

        # Very rough cost estimate for tracking (actual cost from OpenRouter headers is better)
        cost = _estimate_cost(self.model, input_tokens, output_tokens)

        track_ai_generation(
            distinct_id=self.chat_id,
            model=self.model,
            provider=self.provider,
            messages=messages,
            response=response,
            latency_seconds=self.latency,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            metadata=self.metadata,
            http_status=self.http_status,
            base_url=self.base_url,
        )


def _estimate_cost(model: str, input_tok: Optional[int], output_tok: Optional[int]) -> Optional[float]:
    if input_tok is None or output_tok is None:
        return None
    # Rough per-1M-token rates for common models (OpenRouter/OpenAI)
    rates = {
        "gpt-4o-mini": (0.15, 0.60),
        "openai/gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (2.50, 10.00),
        "openai/gpt-4o": (2.50, 10.00),
        "claude-3-haiku": (0.25, 1.25),
        "anthropic/claude-3-haiku": (0.25, 1.25),
        "claude-3-5-sonnet": (3.00, 15.00),
        "anthropic/claude-3-5-sonnet": (3.00, 15.00),
    }
    if model not in rates:
        return None
    inp_rate, out_rate = rates[model]
    return (input_tok * inp_rate + output_tok * out_rate) / 1_000_000
