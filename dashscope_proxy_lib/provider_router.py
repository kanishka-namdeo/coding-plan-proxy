"""Provider routing logic for multi-provider support."""

import logging
import sys
from dataclasses import dataclass

from dashscope_proxy_lib.config import (
    SECONDARY_MODELS, TERTIARY_MODELS, QUATERNARY_MODELS, QUINARY_MODELS, SENARY_MODELS, MODEL_PROVIDER_MAP,
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
    """Build lookup set for tertiary (OpenLux) models, including mimo hyphen aliases."""
    ids: set[str] = set()
    for entry in models.get("data", []):
        model_id = entry["id"]
        ids.add(model_id)
        # mimo-v2.5 moved to tertiary; keep Cursor hyphen alias for direct lookup.
        if model_id.startswith("mimo-v2.5"):
            ids.add("mimo-v2-5" + model_id[len("mimo-v2.5"):])
    return ids


def _build_quaternary_model_ids(models: dict) -> set[str]:
    """Build lookup set for quaternary (ARK) models."""
    return {entry["id"] for entry in models.get("data", [])}


def _build_quinary_model_ids(models: dict) -> set[str]:
    """Build lookup set for quinary (Meta AI) models."""
    return {entry["id"] for entry in models.get("data", [])}


def _build_senary_model_ids(models: dict) -> set[str]:
    """Build lookup set for senary (DeepSeek) models."""
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


def _quinary_cfg(name: str) -> str:
    """Resolve quinary provider config (always reads live config module values)."""
    from dashscope_proxy_lib import config as _c
    return getattr(_c, name, "")


def _senary_cfg(name: str) -> str:
    """Resolve senary provider config (always reads live config module values)."""
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
        quinary_key = _quinary_cfg("QUINARY_API_KEY")
        quinary_base = _quinary_cfg("QUINARY_BASE_URL")
        self.quinary = ProviderConfig(
            name="quinary",
            api_key=quinary_key,
            base_url=quinary_base or _cfg("TARGET_BASE"),
            is_available=bool(quinary_key and quinary_base),
        )
        senary_key = _senary_cfg("SENARY_API_KEY")
        senary_base = _senary_cfg("SENARY_BASE_URL")
        self.senary = ProviderConfig(
            name="senary",
            api_key=senary_key,
            base_url=senary_base or _cfg("TARGET_BASE"),
            is_available=bool(senary_key and senary_base),
        )
        # Cache model IDs for O(1) lookup
        self._secondary_model_ids: set[str] = _build_secondary_model_ids(SECONDARY_MODELS)
        self._tertiary_model_ids: set[str] = _build_tertiary_model_ids(TERTIARY_MODELS)
        self._quaternary_model_ids: set[str] = _build_quaternary_model_ids(QUATERNARY_MODELS)
        self._quinary_model_ids: set[str] = _build_quinary_model_ids(QUINARY_MODELS)
        self._senary_model_ids: set[str] = _build_senary_model_ids(SENARY_MODELS)
        # Overlap registry: bare model_id -> provider names serving it.
        # Reads model lists live from the config module (not the frozen top-level
        # bindings above) so monkeypatch.setattr on dashscope_proxy_lib.config
        # takes effect. Seeded with primary's MOCK_MODELS ids first.
        from dashscope_proxy_lib import config as _live_cfg
        self._overlap_registry: dict[str, list[str]] = {}
        for _entry in _live_cfg.MOCK_MODELS.get("data", []):
            self._overlap_registry.setdefault(_entry["id"], []).append("primary")
        _all_sets = [
            ("senary", _build_senary_model_ids(_live_cfg.SENARY_MODELS)),
            ("quinary", _build_quinary_model_ids(_live_cfg.QUINARY_MODELS)),
            ("quaternary", _build_quaternary_model_ids(_live_cfg.QUATERNARY_MODELS)),
            ("tertiary", _build_tertiary_model_ids(_live_cfg.TERTIARY_MODELS)),
            ("secondary", _build_secondary_model_ids(_live_cfg.SECONDARY_MODELS)),
        ]
        for _pname, _ids in _all_sets:
            if getattr(self, _pname).is_available:
                for _mid in _ids:
                    self._overlap_registry.setdefault(_mid, []).append(_pname)
        self._log_provider_status()

    def _log_provider_status(self) -> None:
        """Log the status of configured providers."""
        _log(logging.INFO, "provider router initialized",
             primary_available=self.primary.is_available,
             secondary_available=self.secondary.is_available,
             tertiary_available=self.tertiary.is_available,
             quaternary_available=self.quaternary.is_available,
             quinary_available=self.quinary.is_available,
             senary_available=self.senary.is_available)

    def is_secondary_configured(self) -> bool:
        """Check if secondary provider is fully configured."""
        return self.secondary.is_available

    def is_tertiary_configured(self) -> bool:
        """Check if tertiary provider is fully configured."""
        return self.tertiary.is_available

    def is_quaternary_configured(self) -> bool:
        """Check if quaternary provider is fully configured."""
        return self.quaternary.is_available

    def is_quinary_configured(self) -> bool:
        """Check if quinary provider is fully configured."""
        return self.quinary.is_available

    def is_senary_configured(self) -> bool:
        """Check if senary provider is fully configured."""
        return self.senary.is_available

    def get_provider_for_model(self, model_name: str) -> ProviderConfig:
        """
        Determine which provider should handle a request for the given model.

        Priority:
        0. Provider pin '<provider>/<model>' (e.g. 'openlux/...') -> pinned provider if configured,
           else fall through to normal resolution on the bare name
        1. Explicit mapping in MODEL_PROVIDER_MAP
        2. Model exists in SENARY_MODELS -> senary
        3. Model exists in QUINARY_MODELS -> quinary
        4. Model exists in QUATERNARY_MODELS -> quaternary
        5. Model exists in TERTIARY_MODELS -> tertiary
        6. Model exists in SECONDARY_MODELS -> secondary
        7. Default to primary
        """
        model_name = normalize_model_name(model_name)

        from dashscope_proxy_lib.request_transform import split_provider_prefix
        pinned, bare = split_provider_prefix(model_name)
        if pinned is not None:
            provider = getattr(self, pinned)
            if provider.is_available:
                return provider
            model_name = bare  # fall through to normal resolution on bare name
        else:
            model_name = bare

        # Check explicit mapping first (highest priority)
        mapped = MODEL_PROVIDER_MAP.get(model_name)
        if mapped == "senary" and self.senary.is_available:
            return self.senary
        if mapped == "quinary" and self.quinary.is_available:
            return self.quinary
        if mapped == "quaternary" and self.quaternary.is_available:
            return self.quaternary
        if mapped == "tertiary" and self.tertiary.is_available:
            return self.tertiary
        if mapped == "secondary" and self.secondary.is_available:
            return self.secondary
        if mapped == "primary":
            return self.primary

        # O(1) set lookup for senary models
        if self.senary.is_available and model_name in self._senary_model_ids:
            return self.senary

        # O(1) set lookup for quinary models
        if self.quinary.is_available and model_name in self._quinary_model_ids:
            return self.quinary

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

    def get_providers_for_model(self, model_name: str) -> list:
        """All configured providers serving the bare model ID, in try-order."""
        from dashscope_proxy_lib.request_transform import split_provider_prefix
        _, bare = split_provider_prefix(model_name)
        names = list(self._overlap_registry.get(bare, []))
        order = _cfg("MODEL_FALLBACK_ORDER") or []
        if order:
            names.sort(key=lambda n: order.index(n) if n in order else len(order))
        return [getattr(self, n) for n in names]

    def get_model_overlaps(self) -> dict:
        """model_id -> provider names, only entries served by 2+ providers."""
        return {m: list(p) for m, p in self._overlap_registry.items() if len(p) > 1}

    def get_all_models(self) -> dict:
        """
        Return combined model list from all configured providers.
        Only includes secondary/tertiary/quaternary/quinary/senary models when those providers are configured.
        Overlapping model IDs are deduped (first occurrence wins); each entry
        carries `providers` (canonical names) and `provider_models` (slugs).
        """
        from dashscope_proxy_lib import config as _live_cfg

        _slug_for = {"primary": "dashscope", "secondary": "mimo", "tertiary": "openlux",
                     "quaternary": "ark", "quinary": "metaspark", "senary": "deepseek"}
        models = {"object": "list", "data": []}
        seen: set[str] = set()
        _sources = [
            ("primary", True, _live_cfg.MOCK_MODELS),
            ("secondary", self.secondary.is_available, _live_cfg.SECONDARY_MODELS),
            ("tertiary", self.tertiary.is_available, _live_cfg.TERTIARY_MODELS),
            ("quaternary", self.quaternary.is_available, _live_cfg.QUATERNARY_MODELS),
            ("quinary", self.quinary.is_available, _live_cfg.QUINARY_MODELS),
            ("senary", self.senary.is_available, _live_cfg.SENARY_MODELS),
        ]
        for _pname, _available, _models in _sources:
            if not _available:
                continue
            for _entry in _models.get("data", []):
                _mid = _entry["id"]
                if _mid in seen:
                    continue
                seen.add(_mid)
                _names = list(self._overlap_registry.get(_mid, [_pname]))
                _copy = dict(_entry)
                _copy["providers"] = _names
                _copy["provider_models"] = [f"{_slug_for[n]}/{_mid}" for n in _names]
                models["data"].append(_copy)

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
            "quinary": {
                "available": self.quinary.is_available,
                "base_url": self.quinary.base_url if self.quinary.is_available else None,
            },
            "senary": {
                "available": self.senary.is_available,
                "base_url": self.senary.base_url if self.senary.is_available else None,
            },
        }
