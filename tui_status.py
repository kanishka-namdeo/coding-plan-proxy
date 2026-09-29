"""Pure helpers that map rate_limiter.status() onto TUI series and visibility."""

from datetime import datetime, timezone


def parse_timestamp_utc(value) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None.

    Tolerates a trailing "Z" (fromisoformat rejects it on older Pythons) and
    treats a naive value as UTC. Returns None for unparseable input so callers
    can treat it as "not usable" (e.g. not recent).
    """
    if not value:
        return None
    text = str(value)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_failover_recent(value, now: datetime | None = None, within_seconds: int = 300) -> bool:
    """Return True if the failover timestamp `value` is within the last window.

    `value` is a `timestamp_utc` string from a session-log entry. Unparseable
    and FUTURE timestamps are treated as NOT recent, so a single old or
    clock-skewed failover does not linger on the alert badge.
    """
    ts = parse_timestamp_utc(value)
    if ts is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    age = (now - ts).total_seconds()
    return 0 <= age <= within_seconds


def is_multi_provider(status: dict) -> bool:
    return isinstance(status.get("primary"), dict)


def series_from_status(status: dict) -> dict:
    """Sparkline / histogram inputs. Prefer MultiProvider top-level globals."""
    if is_multi_provider(status):
        # PROVIDER_KEYS is the single source of truth for the provider set; do
        # not reintroduce a separate literal list here.
        rpm = sum(
            (status.get(k) or {}).get("rpm_current", 0)
            for k in PROVIDER_KEYS
            if isinstance(status.get(k), dict)
        )
        tpm_used = 0
        for k in PROVIDER_KEYS:
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


# Canonical provider ordering/labels for the Overview compact provider grid.
PROVIDER_KEYS = (
    "primary", "secondary", "tertiary", "quaternary", "quinary", "senary", "septenary",
    "octonary", "nonary", "decenary",
)
PROVIDER_LABELS = {
    "primary": "DashScope",
    "secondary": "MIMO",
    "tertiary": "OpenLux",
    "quaternary": "ARK",
    "quinary": "Meta AI",
    "senary": "DeepSeek",
    "septenary": "GLM",
    "octonary": "Agnes Text",
    "nonary": "Agnes Image",
    "decenary": "Agnes Video",
}


def _compact_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def mini_bar(current: int, maximum: int, width: int = 8) -> str:
    """Compact usage cell for the grid: '████░░░░ 42%'.

    Uses block characters — bracketed bars like '[####....]' are eaten by
    Rich console markup when DataTable cells render.

    The displayed percent is clamped to [0, 999]: `current > maximum` (over
    quota) would otherwise show >100% and negative values (TPM available can
    go negative when reservations overshoot) would render "-12%". The fill is
    derived from the same clamped percent so the bar and the label agree.
    """
    if maximum <= 0:
        return f"{'░' * width} 0%"
    clamped_pct = int(max(0, min(999, current / maximum * 100)))
    filled = int(clamped_pct / 100 * width)
    filled = max(0, min(filled, width))
    return f"{'█' * filled}{'░' * (width - filled)} {clamped_pct}%"


def circuit_glyph(provider_status: dict) -> str:
    """Map status dict keys onto the 3-state circuit glyph.

    status() exposes only `circuit_open` + `circuit_failure_count`, so:
    open -> "OPEN", failures recorded (not yet open) -> "half", else "ok".
    """
    if provider_status.get("circuit_open"):
        return "OPEN"
    if provider_status.get("circuit_failure_count", 0) > 0:
        return "half"
    return "ok"


def provider_grid_rows(status: dict) -> list[dict]:
    """Rows for the Overview compact provider grid.

    One row per provider sub-dict present in `status` (MultiProvider
    top-level shape: {"primary": {...}, ...}). Idle providers (no usage
    data) still get a row so configured-but-quiet providers stay visible.
    """
    rows = []
    for key in PROVIDER_KEYS:
        p = status.get(key)
        if not isinstance(p, dict):
            continue
        rpm_current = p.get("rpm_current", 0)
        forwarded = p.get("total_forwarded", 0)
        n429 = p.get("total_429s", 0)
        rejected = p.get("total_rejected", 0)
        idle = forwarded == 0 and rpm_current == 0 and n429 == 0 and rejected == 0
        if idle:
            rpm_cell = "idle"
            tpm_cell = "idle"
        else:
            rpm_cell = mini_bar(rpm_current, p.get("rpm_limit", 0))
            # Both cells mean "used / limit". TPM available can go negative
            # when reservations overshoot, so clamp used to a non-negative
            # value; the full-quota case renders a full bar (like RPM).
            used_tpm = max(0, p.get("tpm_limit", 0) - p.get("tpm_available", 0))
            tpm_cell = mini_bar(used_tpm, p.get("tpm_limit", 0))
        rows.append({
            "key": key,
            "label": PROVIDER_LABELS.get(key, key),
            "circuit": circuit_glyph(p),
            "rpm": rpm_cell,
            "tpm": tpm_cell,
            "forwarded": _compact_number(forwarded),
            "count_429s": str(n429),
            "idle": idle,
        })
    return rows


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


def success_rate_display(total_forwarded: int, total_429s: int, total_rejected: int) -> str:
    """Display string for the Overview Success Rate row.

    With zero attempts (idle proxy) the raw rate is 0.0 which reads as total
    failure; surface "n/a" instead so an idle proxy does not look broken.
    `overview_request_stats` deliberately still returns 0.0 for API stability —
    this display-side guard is what the UI consumes.
    """
    attempts = total_forwarded + total_429s + total_rejected
    if attempts == 0:
        return "n/a"
    return f"{(total_forwarded / attempts * 100):.1f}%"
