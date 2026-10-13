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
    "opencode": "undenary", "undenary": "undenary",
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


def inject_agnes_thinking(body: dict, provider_name: str) -> dict:
    """Force thinking mode for Agnes text (octonary) requests.
    
    Agnes 3.0 Flash defaults to thinking-enabled with xhigh reasoning effort.
    This ensures all octonary requests use maximum reasoning capability.
    """
    if provider_name != "octonary":
        return body
    kwargs = body.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
        body["chat_template_kwargs"] = kwargs
    kwargs["enable_thinking"] = True
    kwargs["reasoning_effort"] = "xhigh"
    return body


def inject_qwen_thinking(body: dict, model_name: str) -> dict:
    """Force thinking mode for Qwen3.5/3.6/3.7/3.8 models with maximum reasoning effort.
    
    Qwen3.5/3.6/3.7 support enable_thinking parameter.
    Qwen3.8 series also supports reasoning_effort with values: xhigh (default), medium, low.
    """
    if not model_name:
        return body
    
    # Qwen3.8 series supports reasoning_effort
    if model_name.startswith("qwen3.8"):
        params = body.get("parameters")
        if not isinstance(params, dict):
            params = {}
            body["parameters"] = params
        params["enable_thinking"] = True
        params["reasoning_effort"] = "xhigh"
    # Qwen3.5/3.6/3.7 series support enable_thinking
    elif model_name.startswith(("qwen3.5", "qwen3.6", "qwen3.7")):
        params = body.get("parameters")
        if not isinstance(params, dict):
            params = {}
            body["parameters"] = params
        params["enable_thinking"] = True
    
    return body


def inject_glm53_thinking(body: dict, model_name: str) -> dict:
    """Force thinking mode for GLM-5.3 models with maximum reasoning effort.
    
    GLM-5.3 series (glm-5.3, glm-5.3-flash) always thinks and supports
    reasoning_effort with values: max (default), high, low.
    """
    if not model_name or not model_name.startswith("glm-5.3"):
        return body
    params = body.get("parameters")
    if not isinstance(params, dict):
        params = {}
        body["parameters"] = params
    params["enable_thinking"] = True
    params["reasoning_effort"] = "max"
    return body


def inject_kimi_thinking(body: dict, model_name: str) -> dict:
    """Force thinking mode for Kimi K2.5/K2.6/K2.7 models.
    
    Kimi K2.5/K2.6/K2.7 use thinking parameter with type: enabled.
    K2.7-code always thinks and cannot be disabled.
    """
    if not model_name or not model_name.startswith("kimi-k2"):
        return body
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        thinking = {}
        body["thinking"] = thinking
    thinking["type"] = "enabled"
    # K2.6 and K2.7 support keep parameter for preserved thinking
    if model_name.startswith(("kimi-k2.6", "kimi-k2.7")):
        thinking["keep"] = "all"
    return body


def inject_mimo_thinking(body: dict, model_name: str) -> dict:
    """Force thinking mode for MIMO v2.5/v2.6 models.
    
    MIMO models (mimo-v2.5-pro, mimo-v2.5, mimo-v2.6-flash, mimo-v2.6-pro, 
    mimo-v2.6-pro-ultraspeed) use thinking parameter with type: enabled.
    Thinking is enabled by default and cannot be disabled during thinking mode.
    """
    if not model_name or not model_name.startswith("mimo-v2"):
        return body
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        thinking = {}
        body["thinking"] = thinking
    thinking["type"] = "enabled"
    return body


def inject_deepseek_thinking(body: dict, model_name: str) -> dict:
    """Force thinking mode for DeepSeek V3.1/V3.2/V4 and Flash models.

    DeepSeek V3.1/V3.2/V4 support enable_thinking parameter.
    V3.1 uses chat_template_kwargs.thinking: true
    V3.2/V4 use enable_thinking: true with optional reasoning_effort.
    deepseek-flash uses a top-level thinking:{type} object plus a
    top-level reasoning_effort knob (max).
    """
    if not model_name:
        return body

    # DeepSeek V3.1 uses chat_template_kwargs.thinking
    if model_name == "deepseek-v3.1":
        kwargs = body.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
            body["chat_template_kwargs"] = kwargs
        kwargs["thinking"] = True
    # DeepSeek V3.2/V4 use enable_thinking with reasoning_effort
    elif model_name.startswith(("deepseek-v3.2", "deepseek-v4")):
        params = body.get("parameters")
        if not isinstance(params, dict):
            params = {}
            body["parameters"] = params
        params["enable_thinking"] = True
        params["reasoning_effort"] = "max"
    # deepseek-flash uses a top-level thinking object + reasoning_effort
    elif model_name == "deepseek-flash":
        thinking = body.get("thinking")
        if not isinstance(thinking, dict):
            thinking = {}
            body["thinking"] = thinking
        thinking["type"] = "enabled"
        body["reasoning_effort"] = "max"

    return body


def inject_glm5_thinking(body: dict, model_name: str) -> dict:
    """Force thinking mode for GLM-5/5.1/5.2 models.
    
    GLM-5/5.1 use chat_template_kwargs.enable_thinking: true
    GLM-5.2 supports reasoning_effort: max (default), high.
    """
    if not model_name:
        return body
    
    # GLM-5.2 supports reasoning_effort
    if model_name == "glm-5.2":
        kwargs = body.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
            body["chat_template_kwargs"] = kwargs
        kwargs["enable_thinking"] = True
        kwargs["reasoning_effort"] = "max"
    # GLM-5 and GLM-5.1 use enable_thinking
    elif model_name in ("glm-5", "glm-5.1"):
        kwargs = body.get("chat_template_kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
            body["chat_template_kwargs"] = kwargs
        kwargs["enable_thinking"] = True
    
    return body


def inject_minimax_thinking(body: dict, model_name: str) -> dict:
    """Force thinking mode for MiniMax M2.5/M3 models.

    MiniMax models use thinking_mode parameter.
    """
    if not model_name or not model_name.startswith("MiniMax-M"):
        return body
    params = body.get("parameters")
    if not isinstance(params, dict):
        params = {}
        body["parameters"] = params
    params["thinking_mode"] = True
    return body


def inject_gpt_thinking(body: dict, model_name: str) -> dict:
    """Force maximum reasoning effort for OpenAI GPT-5.x/GPT-6 models (OpenLux).

    The OpenAI reasoning ladder is none/minimal/low/medium/high/xhigh/max,
    so ``max`` is the ceiling. Exposed as a top-level ``reasoning_effort`` on
    chat completions. Older gpt-4.x is intentionally not matched — it has no
    documented reasoning-effort knob.
    """
    if not model_name or not model_name.startswith(("gpt-5", "gpt-6")):
        return body
    body["reasoning_effort"] = "max"
    return body


def inject_grok_thinking(body: dict, model_name: str) -> dict:
    """Force maximum reasoning effort for xAI Grok models (OpenLux).

    Grok exposes a top-level ``reasoning_effort``; grok-4.6 documents
    low/medium/high/xhigh, and the knob cannot be disabled, so ``xhigh`` is
    the ceiling.
    """
    if not model_name or not model_name.startswith("grok"):
        return body
    body["reasoning_effort"] = "xhigh"
    return body


def inject_muse_thinking(body: dict, model_name: str) -> dict:
    """Force maximum documented reasoning effort for Meta Muse Spark models.

    Muse Spark (quinary / OpenCode Zen free) exposes a top-level
    ``reasoning_effort``. The contributor tier caps at ``xhigh`` and does not
    document the standard-tier-only ``max`` level, so ``xhigh`` is the safe
    ceiling for the whole family.
    """
    if not model_name or not model_name.startswith("muse"):
        return body
    body["reasoning_effort"] = "xhigh"
    return body


def inject_glm4_thinking(body: dict, model_name: str) -> dict:
    """Force thinking on for GLM-4.x / GLM-5-turbo models (GLM / Z.ai).

    These pre-GLM-5.2 Z.ai generations have a thinking toggle but no
    reasoning_effort knob (effort starts at GLM-5.2). They follow the proven
    GLM-5 Z.ai convention of ``chat_template_kwargs.enable_thinking``.
    """
    if not model_name or not model_name.startswith(
        ("glm-4.5", "glm-4.6", "glm-4.7", "glm-5-turbo")
    ):
        return body
    kwargs = body.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
        body["chat_template_kwargs"] = kwargs
    kwargs["enable_thinking"] = True
    return body


def inject_nemotron_thinking(body: dict, model_name: str) -> dict:
    """Force thinking on for NVIDIA Nemotron models (OpenCode Zen free tier).

    Nemotron 3.5 Lightning supports an enable_thinking toggle via
    ``chat_template_kwargs``. Best-effort through the OpenAI-compatible Zen
    gateway — the gateway may drop the knob if it does not forward it.
    """
    if not model_name or not model_name.startswith("nemotron"):
        return body
    kwargs = body.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
        body["chat_template_kwargs"] = kwargs
    kwargs["enable_thinking"] = True
    return body


def inject_ling_thinking(body: dict, model_name: str) -> dict:
    """Force thinking on for Ant/InclusionAI Ling-3 models (OpenCode Zen free).

    Ling-3.1-flash is a hybrid-reasoning model with a top-level
    ``enable_thinking`` boolean. Best-effort through the Zen gateway.
    """
    if not model_name or not model_name.startswith("ling-3"):
        return body
    body["enable_thinking"] = True
    return body


def inject_step5_thinking(body: dict, model_name: str) -> dict:
    """Force maximum documented reasoning effort for StepFun Step-5 models.

    Step-5-preview exposes a top-level ``reasoning_effort`` capped at
    ``"high"``. Best-effort through the Zen gateway.
    """
    if not model_name or not model_name.startswith("step-5"):
        return body
    body["reasoning_effort"] = "high"
    return body


def inject_longcat_thinking(body: dict, model_name: str) -> dict:
    """Force thinking on for Meituan LongCat models (OpenCode Zen free tier).

    LongCat 2.5 uses a top-level ``thinking`` object with ``type: enabled``.
    Best-effort through the Zen gateway.
    """
    if not model_name or not model_name.startswith("longcat"):
        return body
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        thinking = {}
        body["thinking"] = thinking
    thinking["type"] = "enabled"
    return body
