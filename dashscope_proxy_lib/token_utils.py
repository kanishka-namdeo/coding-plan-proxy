"""Token extraction and estimation utilities."""

import json


def extract_tokens_from_response(body: bytes) -> dict:
    """Parse token usage from a JSON response body.

    Returns a dict with all available token fields:
    - total_tokens: int
    - prompt_tokens: int
    - completion_tokens: int
    - input_tokens: int (alias for prompt_tokens if present)
    - output_tokens: int (alias for completion_tokens if present)
    - cached_tokens: int (if present in input_tokens_details or similar)
    """
    try:
        data = json.loads(body)
        usage = data.get("usage", {})
        result = {
            "total_tokens": usage.get("total_tokens", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
            "cached_tokens": 0,
        }
        # Check for cached tokens in nested details
        input_details = usage.get("input_tokens_details", {})
        if isinstance(input_details, dict):
            result["cached_tokens"] = input_details.get("cached_tokens", 0)
        # Also check direct field
        if "cached_tokens" in usage:
            result["cached_tokens"] = usage["cached_tokens"]
        return result
    except (json.JSONDecodeError, AttributeError):
        return {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
        }


def extract_tokens_from_stream(buffer: bytes) -> dict:
    """Parse token usage from accumulated SSE stream buffer.

    Returns a dict with all available token fields (see extract_tokens_from_response).
    """
    try:
        lines = buffer.decode("utf-8", errors="replace").split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                data = json.loads(line[6:])
                usage = data.get("usage", {})
                if usage and "total_tokens" in usage:
                    result = {
                        "total_tokens": usage.get("total_tokens", 0),
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                        "cached_tokens": 0,
                    }
                    input_details = usage.get("input_tokens_details", {})
                    if isinstance(input_details, dict):
                        result["cached_tokens"] = input_details.get("cached_tokens", 0)
                    if "cached_tokens" in usage:
                        result["cached_tokens"] = usage["cached_tokens"]
                    return result
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
    }


def estimate_tokens_for_request(body_bytes: bytes) -> int:
    """Rough estimate of tokens in the request body for TPM planning."""
    try:
        body = json.loads(body_bytes)
        messages = body.get("messages", [])
        if not isinstance(messages, list):
            return 100
        total_chars = sum(
            len(m.get("content", ""))
            for m in messages
            if isinstance(m.get("content"), str)
        )
        return max(100, total_chars // 4)
    except (json.JSONDecodeError, AttributeError):
        return 100
