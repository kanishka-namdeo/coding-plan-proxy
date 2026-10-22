"""Token extraction and estimation utilities."""

import json

# Media parts contribute a fixed char allowance to the token estimate.
_MEDIA_PART_TYPES = frozenset({"image_url", "image", "input_audio", "audio"})
_MEDIA_PART_CHARS = 4096


def _safe_int(value, default: int = 0) -> int:
    """Coerce an upstream usage value to ``int``.

    Providers occasionally report ``"total_tokens": "12345"`` or ``null``.
    Left uncoerced these reach arithmetic in the handler (``actual -
    estimated``) and raise ``TypeError``, which is not a caught upstream
    error, so the request 500s with its TPM reservation still held.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _count_content_chars(content) -> int:
    """Count char allowance for a message/system content value.

    - str: its length
    - list: str parts count their length; dict parts count their ``text``
      field when it is a string; media parts (``type`` in
      {_MEDIA_PART_TYPES} or an ``image_url`` key) add a fixed allowance.
    - anything else: 0
    """
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if isinstance(part, str):
            total += len(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                total += len(text)
            if part.get("type") in _MEDIA_PART_TYPES or "image_url" in part:
                total += _MEDIA_PART_CHARS
    return total


def _empty_tokens() -> dict:
    return {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
    }


def _tokens_from_usage(usage: dict) -> dict:
    """Build a token dict from a provider ``usage`` block, coercing every count."""
    if not isinstance(usage, dict):
        return _empty_tokens()
    result = {
        "total_tokens": _safe_int(usage.get("total_tokens", 0)),
        "prompt_tokens": _safe_int(usage.get("prompt_tokens", 0)),
        "completion_tokens": _safe_int(usage.get("completion_tokens", 0)),
        "input_tokens": _safe_int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
        "output_tokens": _safe_int(usage.get("output_tokens", usage.get("completion_tokens", 0))),
        "cached_tokens": 0,
    }
    # Check for cached tokens in nested details
    input_details = usage.get("input_tokens_details", {})
    if isinstance(input_details, dict):
        result["cached_tokens"] = _safe_int(input_details.get("cached_tokens", 0))
    # Also check direct field
    if "cached_tokens" in usage:
        result["cached_tokens"] = _safe_int(usage["cached_tokens"])
    return result


def extract_tokens_from_response(body: bytes) -> dict:
    """Parse token usage from a JSON response body.

    Returns a dict with all available token fields, all coerced to ``int``:
    - total_tokens: int
    - prompt_tokens: int
    - completion_tokens: int
    - input_tokens: int (alias for prompt_tokens if present)
    - output_tokens: int (alias for completion_tokens if present)
    - cached_tokens: int (if present in input_tokens_details or similar)
    """
    try:
        data = json.loads(body)
        return _tokens_from_usage(data.get("usage", {}))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return _empty_tokens()


def extract_tokens_from_stream(buffer: bytes) -> dict:
    """Parse token usage from accumulated SSE stream buffer.

    Returns a dict with all available token fields (see extract_tokens_from_response).
    Malformed ``data:`` lines are skipped, not fatal — a provider heartbeat or
    a truncated trailing line must not discard a usage line already seen.
    """
    try:
        lines = buffer.decode("utf-8", errors="replace").split("\n")
    except UnicodeDecodeError:  # pragma: no cover - errors="replace" above cannot raise
        return _empty_tokens()
    for line in reversed(lines):
        line = line.strip()
        if not (line.startswith("data:") and line[5:].lstrip() != "[DONE]"):
            continue
        try:
            data = json.loads(line[5:].lstrip())
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue  # malformed line — keep scanning backwards for a real usage line
        if not isinstance(data, dict):
            continue
        usage = data.get("usage", {})
        if usage and "total_tokens" in usage:
            return _tokens_from_usage(usage)
    return _empty_tokens()


def estimate_tokens_for_body(body: dict) -> int:
    """Rough estimate from an already-parsed request body for TPM planning."""
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return 100

    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        total_chars += _count_content_chars(m.get("content"))

    # system / developer may be a plain string or a list of content parts
    for field in ("system", "developer"):
        total_chars += _count_content_chars(body.get(field))

    tools = body.get("tools", [])
    if isinstance(tools, list):
        try:
            total_chars += len(json.dumps(tools))
        except (TypeError, ValueError):
            total_chars += len(tools) * 200

    return max(100, total_chars // 4)


def estimate_tokens_for_request(body_bytes: bytes) -> int:
    """Rough estimate of tokens in the request body for TPM planning."""
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return 100
    if not isinstance(body, dict):
        return 100
    return estimate_tokens_for_body(body)
