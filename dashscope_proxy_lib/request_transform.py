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

    The input is stripped and the MIMO alias prefix is matched
    case-insensitively; the suffix after the alias prefix is preserved
    as-is.
    """
    if not isinstance(model_name, str):
        return model_name
    stripped = model_name.strip()
    _alias = "mimo-v2-5"
    if stripped.lower().startswith(_alias):
        return "mimo-v2.5" + stripped[len(_alias):]
    return stripped


PROVIDER_SLUG_MAP = {
    "dashscope": "primary", "primary": "primary",
    "mimo": "secondary", "secondary": "secondary",
    "openlux": "tertiary", "tertiary": "tertiary",
    "ark": "quaternary", "quaternary": "quaternary",
    "metaspark": "quinary", "quinary": "quinary",
    "deepseek": "senary", "senary": "senary",
    "glm": "septenary", "zai": "septenary", "septenary": "septenary",
    "agnes": "octonary", "octonary": "octonary",
    "agnes-image": "nonary", "nonary": "nonary",
    "agnes-video": "decenary", "decenary": "decenary",
}


def split_provider_prefix(model: str) -> tuple:
    """Split optional '<provider>/<model>' prefix. Returns (provider_or_None, bare_model)."""
    if "/" not in model:
        return None, model
    head, _, tail = model.partition("/")
    provider = PROVIDER_SLUG_MAP.get(head.lower())
    if provider is None or not tail:
        return None, model
    return provider, normalize_model_name(tail)


def _is_chat_endpoint(path: str) -> bool:
    """Check if path is a chat completion endpoint."""
    return "chat/completions" in path.lower()


# Generation endpoints (image/video) carry a prompt/size payload instead of a
# chat `messages` array and must not be rejected by the chat-style validation.
GENERATION_PATH_MARKERS = ("images/generations", "images/edits", "videos")


def requires_messages(path: str) -> bool:
    """Chat-style endpoints require a non-empty `messages` array; generation
    endpoints carry a prompt/size payload instead. Unknown paths stay strict."""
    lowered = (path or "").lower()
    return not any(marker in lowered for marker in GENERATION_PATH_MARKERS)
