"""Provider routing logic for multi-provider support."""

import logging
import sys
from dataclasses import dataclass

from dashscope_proxy_lib.config import (
    SECONDARY_MODELS, TERTIARY_MODELS, QUATERNARY_MODELS, MODEL_PROVIDER_MAP,
)
from dashscope_proxy_lib.logging_config import _log
from dashscope_proxy_lib.request_transform import normalize_model_name


def _build_secondary_model_ids(models: dict) -> set[str]:
    """Build lookup set including Cursor hyphen aliases (mimo-v2-5-*)."""
    ids: set[str] = set()
    for entry in models.get("data", []):
        model_id = entry["id"]
        ids.add(model_id)
        # Cursor sends MIMO v2.5 models with hyphens; include alias for direct lookup.
        if model_id.startswith("mimo-v2.5"):
            ids.add("mimo-v2-5" + model_id[len("mimo-v2.5"):])
    return ids


def _build_tertiary_model_ids(models: dict) -> set[str]:
    """Build lookup set for tertiary (StreamLake) models."""
    return {entry["id"] for entry in models.get("data", [])}


def _build_quaternary_model_ids(models: dict) -> set[str]:
    """Build lookup set for quaternary (ARK) models."""
    return {entry["id"] for entry in models.get("data", [])}


def _cfg(name: str):
    """Resolve a constant through the facade module (supports runtime patching by tests)."""
    _ds = sys.modules.get("dashscope_proxy")
    if _ds is not None:
        return getattr(_ds, name)
    from dashscope_proxy_lib import config as _c
    return getattr(_c, name)


def _secondary_cfg(name: str) -> str:
    """Resolve secondary provider config (always reads live config module values)."""
    from dashscope_proxy_lib import config as _c
    return getattr(_c, name, "")


def _tertiary_cfg(name: str) -> str:
    """Resolve tertiary provider config (always reads live config module values)."""
    from dashscope_proxy_lib import config as _c
    return getattr(_c, name, "")


def _quaternary_cfg(name: str) -> str:
    """Resolve quaternary provider config (always reads live config module values)."""
    from dashscope_proxy_lib import config as _c
    return getattr(_c, name, "")


@dataclass
class ProviderConfig:
    """Configuration for a single provider."""
    name: str
    api_key: str
    base_url: str
    is_available: bool


class ProviderRouter:
    """
    Routes requests to the appropriate provider based on model name.
    Falls back to primary provider when secondary/tertiary are not configured.
    """

    def __init__(self):
        # Resolve through facade so test patches on dashscope_proxy.TARGET_BASE are respected
        self.primary = ProviderConfig(
            name="primary",
            api_key=_cfg("DASHSCOPE_API_KEY"),
            base_url=_cfg("TARGET_BASE"),
            is_available=bool(_cfg("DASHSCOPE_API_KEY")),
        )
        secondary_key = _secondary_cfg("SECONDARY_API_KEY")
        secondary_base = _secondary_cfg("SECONDARY_BASE_URL")
        self.secondary = ProviderConfig(
            name="secondary",
            api_key=secondary_key,
            base_url=secondary_base or _cfg("TARGET_BASE"),
            is_available=bool(secondary_key and secondary_base),
        )
        tertiary_key = _tertiary_cfg("TERTIARY_API_KEY")
        tertiary_base = _tertiary_cfg("TERTIARY_BASE_URL")
        self.tertiary = ProviderConfig(
            name="tertiary",
            api_key=tertiary_key,
            base_url=tertiary_base or _cfg("TARGET_BASE"),
            is_available=bool(tertiary_key and tertiary_base),
        )
        quaternary_key = _quaternary_cfg("QUATERNARY_API_KEY")
        quaternary_base = _quaternary_cfg("QUATERNARY_BASE_URL")
        self.quaternary = ProviderConfig(
            name="quaternary",
            api_key=quaternary_key,
            base_url=quaternary_base or _cfg("TARGET_BASE"),
            is_available=bool(quaternary_key and quaternary_base),
        )
        # Cache model IDs for O(1) lookup
        self._secondary_model_ids: set[str] = _build_secondary_model_ids(SECONDARY_MODELS)
        self._tertiary_model_ids: set[str] = _build_tertiary_model_ids(TERTIARY_MODELS)
        self._quaternary_model_ids: set[str] = _build_quaternary_model_ids(QUATERNARY_MODELS)
        self._log_provider_status()

    def _log_provider_status(self) -> None:
        """Log the status of configured providers."""
        _log(logging.INFO, "provider router initialized",
             primary_available=self.primary.is_available,
             secondary_available=self.secondary.is_available,
             tertiary_available=self.tertiary.is_available,
             quaternary_available=self.quaternary.is_available)

    def is_secondary_configured(self) -> bool:
        """Check if secondary provider is fully configured."""
        return self.secondary.is_available

    def is_tertiary_configured(self) -> bool:
        """Check if tertiary provider is fully configured."""
        return self.tertiary.is_available

    def is_quaternary_configured(self) -> bool:
        """Check if quaternary provider is fully configured."""
        return self.quaternary.is_available

    def get_provider_for_model(self, model_name: str) -> ProviderConfig:
        """
        Determine which provider should handle a request for the given model.

        Priority:
        1. Explicit mapping in MODEL_PROVIDER_MAP
        2. Model exists in QUATERNARY_MODELS -> quaternary
        3. Model exists in TERTIARY_MODELS -> tertiary
        4. Model exists in SECONDARY_MODELS -> secondary
        5. Default to primary
        """
        model_name = normalize_model_name(model_name)

        # Check explicit mapping first (highest priority)
        mapped = MODEL_PROVIDER_MAP.get(model_name)
        if mapped == "quaternary" and self.quaternary.is_available:
            return self.quaternary
        if mapped == "tertiary" and self.tertiary.is_available:
            return self.tertiary
        if mapped == "secondary" and self.secondary.is_available:
            return self.secondary
        if mapped == "primary":
            return self.primary

        # O(1) set lookup for quaternary models
        if self.quaternary.is_available and model_name in self._quaternary_model_ids:
            return self.quaternary

        # O(1) set lookup for tertiary models
        if self.tertiary.is_available and model_name in self._tertiary_model_ids:
            return self.tertiary

        # O(1) set lookup for secondary models
        if self.secondary.is_available and model_name in self._secondary_model_ids:
            return self.secondary

        # Default to primary
        return self.primary

    def get_all_models(self) -> dict:
        """
        Return combined model list from all configured providers.
        Only includes secondary/tertiary/quaternary models when those providers are configured.
        """
        from dashscope_proxy_lib.config import MOCK_MODELS

        models = {"object": "list", "data": []}

        # Always include primary models
        models["data"].extend(MOCK_MODELS.get("data", []))

        # Include secondary models only if configured
        if self.secondary.is_available:
            models["data"].extend(SECONDARY_MODELS.get("data", []))

        # Include tertiary models only if configured
        if self.tertiary.is_available:
            models["data"].extend(TERTIARY_MODELS.get("data", []))

        # Include quaternary models only if configured
        if self.quaternary.is_available:
            models["data"].extend(QUATERNARY_MODELS.get("data", []))

        return models

    def get_provider_status(self) -> dict:
        """Return status of all providers for health checks."""
        return {
            "primary": {
                "available": self.primary.is_available,
                "base_url": self.primary.base_url,
            },
            "secondary": {
                "available": self.secondary.is_available,
                "base_url": self.secondary.base_url if self.secondary.is_available else None,
            },
            "tertiary": {
                "available": self.tertiary.is_available,
                "base_url": self.tertiary.base_url if self.tertiary.is_available else None,
            },
            "quaternary": {
                "available": self.quaternary.is_available,
                "base_url": self.quaternary.base_url if self.quaternary.is_available else None,
            },
        }
