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


def normalize_model_name(model_name: str) -> str:
    """Map client model aliases to canonical upstream model IDs.

    Cursor and other clients often send MIMO v2.5 models with hyphens
    (e.g. ``mimo-v2-5-pro``) while the upstream API expects dots
    (``mimo-v2.5-pro``).
    """
    if model_name.startswith("mimo-v2-5"):
        return "mimo-v2.5" + model_name[len("mimo-v2-5"):]
    return model_name


def _is_chat_endpoint(path: str) -> bool:
    """Check if path is a chat completion endpoint."""
    return "chat/completions" in path.lower()
