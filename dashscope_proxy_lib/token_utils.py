"""Token extraction and estimation utilities."""

import json


def extract_tokens_from_response(body: bytes) -> int:
    """Parse token usage from a JSON response body."""
    try:
        data = json.loads(body)
        usage = data.get("usage", {})
        return usage.get("total_tokens", 0)
    except (json.JSONDecodeError, AttributeError):
        return 0


def extract_tokens_from_stream(buffer: bytes) -> int:
    """Parse token usage from accumulated SSE stream buffer."""
    try:
        lines = buffer.decode("utf-8", errors="replace").split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                data = json.loads(line[6:])
                usage = data.get("usage", {})
                if usage and "total_tokens" in usage:
                    return usage["total_tokens"]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return 0


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
