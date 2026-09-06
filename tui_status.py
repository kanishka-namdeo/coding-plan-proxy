"""Pure helpers that map rate_limiter.status() onto TUI series and visibility."""


def is_multi_provider(status: dict) -> bool:
    return isinstance(status.get("primary"), dict)


def series_from_status(status: dict) -> dict:
    """Sparkline / histogram inputs. Prefer MultiProvider top-level globals."""
    if is_multi_provider(status):
        rpm = sum(
            (status.get(k) or {}).get("rpm_current", 0)
            for k in ("primary", "secondary", "tertiary", "quaternary", "quinary", "senary")
            if isinstance(status.get(k), dict)
        )
        tpm_used = 0
        for k in ("primary", "secondary", "tertiary", "quaternary", "quinary", "senary"):
            p = status.get(k)
            if isinstance(p, dict):
                tpm_used += p.get("tpm_limit", 0) - p.get("tpm_available", 0)
        return {
            "rpm": rpm,
            "tpm_used": tpm_used,
            "queue_depth": status.get("pending_requests", 0),
            "recent_latencies": list(status.get("recent_latencies") or []),
        }
    return {
        "rpm": status.get("rpm_current", 0),
        "tpm_used": status.get("tpm_limit", 0) - status.get("tpm_available", 0),
        "queue_depth": status.get("pending_requests", 0),
        "recent_latencies": list(status.get("recent_latencies") or []),
    }


def provider_section_visible(provider_status: dict | None) -> bool:
    return isinstance(provider_status, dict)


def failover_alert_should_show(existing_warnings: list[str], recent_failovers: bool) -> bool:
    return recent_failovers


def overview_request_stats(status: dict) -> dict:
    if is_multi_provider(status):
        fwd = status.get("total_forwarded", 0)
        n429 = status.get("total_429s", 0)
        rejected = status.get("total_rejected", 0)
        pending = status.get("pending_requests", 0)
        max_q = status.get("max_queue_size", 0)
        if max_q == 0:
            primary = status.get("primary") or {}
            max_q = primary.get("max_queue_size", 0)
    else:
        fwd = status.get("total_forwarded", 0)
        n429 = status.get("total_429s", 0)
        rejected = status.get("total_rejected", 0)
        pending = status.get("pending_requests", 0)
        max_q = status.get("max_queue_size", 0)
    attempts = fwd + n429 + rejected
    success = (fwd / attempts * 100) if attempts else 0.0
    return {
        "total_forwarded": fwd,
        "total_429s": n429,
        "total_rejected": rejected,
        "pending_requests": pending,
        "max_queue_size": max_q,
        "success_rate": success,
    }
