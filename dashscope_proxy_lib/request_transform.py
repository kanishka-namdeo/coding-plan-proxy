"""Request transformation utilities."""

import logging

from dashscope_proxy_lib.logging_config import _log


def map_developer_to_system(body: dict) -> dict:
    """Convert 'developer' role to 'system', handling multi-modal and malformed messages."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        if messages is not None:
            _log(logging.WARNING, "messages field is not a list, skipping role mapping",
                 messages_type=type(messages).__name__)
        return body
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "developer":
            msg["role"] = "system"
        elif not isinstance(msg, dict):
            _log(logging.WARNING, "Non-dict message entry found, skipping",
                 entry_type=type(msg).__name__)
    return body


def _is_chat_endpoint(path: str) -> bool:
    """Check if path is a chat completion endpoint."""
    return "chat/completions" in path.lower()
