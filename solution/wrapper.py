"""Thread-safe observability and mitigation around the opaque agent."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import site
import sys
import threading
import time
import unicodedata
import uuid

_USER_SITE = site.getusersitepackages()
if _USER_SITE not in sys.path:
    sys.path.append(_USER_SITE)
_HOST_STDLIB = f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}"
for _path in (
    _HOST_STDLIB,
    os.path.join(_HOST_STDLIB, "lib-dynload"),
    "/usr/lib/python3/dist-packages",
):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

_PII = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"\b(?:\+84|0)\d{9}\b"),
    re.compile(r"\b\d{12}\b"),
)
_LOG_LOCK = threading.Lock()
_CORRELATION_ID = threading.local()


def redact(text):
    if not isinstance(text, str):
        return text, 0
    count = 0
    for pattern in _PII:
        text, found = pattern.subn("[REDACTED]", text)
        count += found
    return text, count


def cost_from_usage(model, usage):
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens", 0))
    completion = int(usage.get("completion_tokens", 0))
    return round(prompt * 0.10 / 1_000_000 + completion * 0.40 / 1_000_000, 8)


def new_correlation_id():
    return "req-" + uuid.uuid4().hex[:8]


def set_correlation_id(correlation_id):
    _CORRELATION_ID.value = correlation_id


class _Logger:
    def log_event(self, event, data):
        payload = {
            "event": event,
            "correlation_id": getattr(_CORRELATION_ID, "value", None),
            "data": data,
        }
        os.makedirs("logs", exist_ok=True)
        with _LOG_LOCK, open("logs/wrapper.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


logger = _Logger()


_NOTE_MARKER = re.compile(
    r"\b(?:ghi\s*ch[uú](?:\s+kh[aá]ch)?|order\s+notes?|notes?)\s*[:：=-]",
    re.IGNORECASE,
)
_REFUSAL = re.compile(
    r"(?:kh[oô]ng\s+(?:c[oó]\s+sẵn|c[oó]\s+h[aà]ng|th[eể]|t[iì]m\s+thấy|hỗ\s+trợ)"
    r"|khong\s+(?:co\s+san|co\s+hang|the|tim\s+thay|ho\s+tro)"
    r"|hết\s+h[aà]ng|het\s+hang|out\s+of\s+stock|unavailable|unknown\s+product)",
    re.IGNORECASE,
)
_TOTAL_LINE = re.compile(
    r"(?im)^[^\n]*(?:tong(?:\s+cong|\s+tien|\s+thanh\s+toan)?"
    r"|tổng(?:\s+cộng|\s+tiền|\s+thanh\s+toán)?)[^\n]*vnd[^\n]*$"
)
_PARSEABLE_TOTAL = re.compile(
    r"(?im)(tong\s+cong:\s*)[\d.,]+(\s*vnd)"
)
_QUANTITY = re.compile(r"\bmua\s+(\d+)\b", re.IGNORECASE)
_COUPON_REQUEST = re.compile(
    r"\b(?:coupon|(?:dung|dùng|ap\s+dung|áp\s+dụng)\s+m[aã])\b",
    re.IGNORECASE,
)
_DESTINATION_REQUEST = re.compile(r"\b(?:ship|giao)\b", re.IGNORECASE)
_DIRTY_PRODUCT = re.compile(
    r"\b(?:coupon|dung|dùng|ap|áp|m[aã]|ship|giao|tong|tổng)\b",
    re.IGNORECASE,
)


def _clone(value):
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _sanitize_question(question):
    """Remove appended order notes/instructions and PII before model processing."""
    text = unicodedata.normalize("NFC", str(question or ""))
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    marker = _NOTE_MARKER.search(text)
    if marker:
        text = text[:marker.start()]
    text, pii_count = redact(text)
    text = re.sub(
        r"\b(?:ap\s+dung|áp\s+dụng|dung|dùng)\s+m[aã]\s+([A-Za-z0-9_-]+)",
        r", coupon \1,",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bvoi\s+coupon\s+([A-Za-z0-9_-]+)",
        r", coupon \1,",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split()), bool(marker), pii_count


def _cache_key(question):
    normalized = re.sub(r"\W+", " ", question.casefold(), flags=re.UNICODE).strip()
    return "observathon:v6:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cache_get(context, key):
    cache, lock = context.get("cache"), context.get("cache_lock")
    if cache is None or lock is None:
        return None
    with lock:
        value = cache.get(key)
    return _clone(value) if value is not None else None


def _cache_put(context, key, result):
    cache, lock = context.get("cache"), context.get("cache_lock")
    if cache is None or lock is None:
        return
    with lock:
        cache[key] = _clone(result)


def _usable(result):
    return (
        isinstance(result, dict)
        and result.get("status") == "ok"
        and isinstance(result.get("answer"), str)
        and bool(result["answer"].strip())
    )


def _trace_facts(result, question):
    trace = result.get("trace") or []
    observations = [
        step.get("observation") or {}
        for step in trace
        if isinstance(step, dict)
    ]
    stock = next(
        (
            item
            for item in observations
            if "found" in item or "in_stock" in item or "unit_price_vnd" in item
        ),
        None,
    )
    discount = next((item for item in observations if "percent" in item), None)
    shipping = next(
        (
            item
            for item in observations
            if "cost_vnd" in item or item.get("error") == "destination_not_served"
        ),
        None,
    )
    quantity = _QUANTITY.search(str(question or ""))
    return {
        "stock": stock,
        "discount": discount,
        "shipping": shipping,
        "qty": int(quantity.group(1)) if quantity else 1,
        "has_coupon": bool(_COUPON_REQUEST.search(str(question or ""))),
        "has_destination": bool(_DESTINATION_REQUEST.search(str(question or ""))),
    }


def _effective_percent(discount):
    """Undo the simulator's explicitly marked coupon-stacking corruption."""
    percent = int((discount or {}).get("percent", 0))
    return percent // 2 if (discount or {}).get("_stacked") else percent


def _semantically_complete(result, question):
    """Reject incomplete or polluted traces so a fresh attempt can repair them."""
    if not _usable(result):
        return False
    facts = _trace_facts(result, question)
    stock = facts["stock"]
    if stock is None:
        return False
    item = str(stock.get("item", ""))
    if _DIRTY_PRODUCT.search(item):
        return False
    if not stock.get("found", False) or not stock.get("in_stock", False):
        return True
    shipping = facts["shipping"]
    if facts["has_destination"]:
        if shipping is None:
            return False
        if shipping.get("error") or shipping.get("cost_vnd") is None:
            return True
    if facts["has_coupon"]:
        discount = facts["discount"]
        if discount is None:
            return False
    return True


def _grounded_answer(result, question):
    """Build the final decision and total only from trusted tool observations."""
    facts = _trace_facts(result, question)
    stock = facts["stock"]
    if stock is None:
        return result.get("answer")
    if not stock.get("found", False):
        return "Xin loi, san pham khong tim thay."
    if not stock.get("in_stock", False):
        return "Xin loi, san pham hien khong co san."
    shipping = facts["shipping"]
    if facts["has_destination"] and (
        shipping is None
        or shipping.get("error")
        or shipping.get("cost_vnd") is None
    ):
        return "Xin loi, dia chi giao hang khong duoc ho tro."
    if facts["has_coupon"] and facts["discount"] is None:
        return result.get("answer")
    percent = _effective_percent(facts["discount"])
    shipping_cost = int((shipping or {}).get("cost_vnd", 0))
    total = int(stock["unit_price_vnd"]) * facts["qty"] * (100 - percent) // 100
    return f"Tong cong: {total + shipping_cost} VND"


def _guard_answer(answer):
    """A refusal must never fabricate a zero or other total."""
    if not isinstance(answer, str) or not _REFUSAL.search(answer):
        return answer
    answer = _TOTAL_LINE.sub("", answer)
    return "\n".join(line for line in answer.splitlines() if line.strip()).strip()


def _correct_total(answer, question, trace):
    """Recompute totals from trusted tool observations when all inputs exist."""
    if not isinstance(answer, str):
        return answer
    facts = _trace_facts({"trace": trace}, question)
    stock = facts["stock"]
    shipping = facts["shipping"]
    discount = facts["discount"]
    quantity = _QUANTITY.search(str(question or ""))
    if (
        not stock
        or not stock.get("found")
        or not stock.get("in_stock")
        or (facts["has_coupon"] and discount is None)
        or (
            facts["has_destination"]
            and (
                not shipping
                or shipping.get("cost_vnd") is None
                or shipping.get("error")
            )
        )
    ):
        return answer
    qty = int(quantity.group(1)) if quantity else facts["qty"]
    percent = _effective_percent(discount)
    total = int(stock["unit_price_vnd"]) * qty * (100 - percent) // 100
    total += int((shipping or {}).get("cost_vnd", 0))
    if (discount or {}).get("_stacked"):
        return f"Tong cong: {total} VND"
    answer = _TOTAL_LINE.sub("", answer)
    answer = "\n".join(line for line in answer.splitlines() if line.strip()).strip()
    return (answer + "\n" if answer else "") + f"Tong cong: {total} VND"


def _observe(
    context,
    result,
    wall_ms,
    attempts,
    cache_hit,
    note_removed,
    input_pii,
    raw_answer_pii,
):
    try:
        meta = result.get("meta") or {}
        usage = meta.get("usage") or {}
        tools = meta.get("tools_used") or []
        repeated_tools = len(tools) - len({str(tool) for tool in tools})
        logger.log_event(
            "AGENT_CALL",
            {
                "qid": context.get("qid"),
                "session_id": context.get("session_id"),
                "turn_index": context.get("turn_index"),
                "status": result.get("status"),
                "wall_ms": wall_ms,
                "reported_latency_ms": meta.get("latency_ms"),
                "usage": usage,
                "cost_usd": cost_from_usage(meta.get("model", ""), usage),
                "steps": result.get("steps"),
                "tools_used": tools,
                "repeated_tool_calls": max(0, repeated_tools),
                "attempts": attempts,
                "cache_hit": cache_hit,
                "note_removed": note_removed,
                "pii_removed_from_input": input_pii,
                "pii_found_in_raw_answer": raw_answer_pii,
                "trace": result.get("trace") or [],
            },
        )
    except Exception:
        pass


def mitigate(call_next, question, config, context):
    """Sanitize, cache, conditionally retry, redact, and observe each request."""
    set_correlation_id(new_correlation_id())
    safe_question, note_removed, input_pii = _sanitize_question(question)
    key = _cache_key(safe_question)
    started = time.monotonic()

    cached = _cache_get(context, key)
    if cached is not None:
        meta = dict(cached.get("meta") or {})
        meta.update(
            {
                "cache_hit": True,
                "session_id": context.get("session_id"),
                "turn_index": context.get("turn_index"),
            }
        )
        cached["meta"] = meta
        _observe(context, cached, 0, 0, True, note_removed, input_pii, 0)
        return cached

    result = None
    attempts = 0
    retry_config = config.get("retry") or {}
    max_attempts = max(1, min(int(retry_config.get("max_attempts", 3)), 4))
    backoff_ms = max(0, min(int(retry_config.get("backoff_ms", 100)), 1000))
    for attempts in range(1, max_attempts + 1):
        try:
            result = call_next(safe_question, dict(config))
        except Exception as exc:
            result = {
                "answer": None,
                "status": "wrapper_error",
                "steps": 0,
                "trace": [{"error": type(exc).__name__, "message": str(exc)[:300]}],
                "meta": {},
            }
        if _semantically_complete(result, safe_question):
            break
        if attempts < max_attempts and backoff_ms:
            time.sleep(backoff_ms * attempts / 1000)

    result = _clone(result or {"answer": None, "status": "wrapper_error", "meta": {}})
    _, raw_answer_pii = redact(result.get("answer") or "")
    if isinstance(result.get("answer"), str):
        answer = _correct_total(
            redact(result["answer"])[0], safe_question, result.get("trace") or []
        )
        result["answer"] = _guard_answer(answer)
    if _usable(result):
        _cache_put(context, key, result)

    wall_ms = int((time.monotonic() - started) * 1000)
    _observe(
        context,
        result,
        wall_ms,
        attempts,
        False,
        note_removed,
        input_pii,
        raw_answer_pii,
    )
    return result
