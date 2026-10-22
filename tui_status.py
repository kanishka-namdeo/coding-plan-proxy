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
    "octonary", "nonary", "decenary", "undenary",
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
    "undenary": "OpenCode",
}


_COMPACT_UNITS = ((1_000, "K"), (1_000_000, "M"), (1_000_000_000, "B"),
                  (1_000_000_000_000, "T"))
_COMPACT_SUFFIX_NEXT = {"K": "M", "M": "B", "B": "T"}


def _compact_number(n: int) -> str:
    """Shorten a counter for a narrow grid column; never wider than 6 chars.

    Unit continuity matters: a naive `f"{n / 1_000:.1f}K"` renders 999_999 as
    "1000.0K" — both 7 chars (which overflows the 7-wide grid columns) and
    wrong, since 999_999 is 1.0M. After rounding, a mantissa that has reached
    1000 belongs in the next unit; the suffix saturates at "T".
    """
    if n < 1_000:
        return str(n)
    factor, suffix = _COMPACT_UNITS[-1]
    for candidate_factor, candidate_suffix in _COMPACT_UNITS:
        if n >= candidate_factor:
            factor, suffix = candidate_factor, candidate_suffix
            break
    mantissa = n / factor
    # Suffix saturates at "T"; magnitude beyond that is deliberately NOT
    # preserved, because the <= 6-char bound is what lets the Overview grid
    # size its RPM/TPM columns (cell_padding=0). A counter that reaches 10**15
    # cannot happen through a local proxy, so the cap wins over the magnitude.
    while round(mantissa, 1) >= 1000:
        mantissa /= 1000
        suffix = _COMPACT_SUFFIX_NEXT.get(suffix, "T")
    return f"{mantissa:.1f}{suffix}"


# Block width of the Overview grid's usage bars. Worst-case cell length is
# `OVER_QUOTA_MARK + bar + " " + "999" + "%"` = GRID_BAR_WIDTH + 6, exported as
# GRID_BAR_CELL_MAX — the RPM/TPM column widths in `proxy_tui.py` are sized from
# that value plus one cell of slack.
GRID_BAR_WIDTH = 5
OVER_QUOTA_MARK = "!"
GRID_BAR_CELL_MAX = GRID_BAR_WIDTH + 6


def mini_bar(current: int, maximum: int, width: int = GRID_BAR_WIDTH) -> str:
    """Compact usage cell for the grid: '████░░░░ 42%'.

    Uses block characters — bracketed bars like '[####....]' are eaten by
    Rich console markup when DataTable cells render.

    The displayed percent is clamped to [0, 999]: `current > maximum` (over
    quota) would otherwise show >100% and negative values (TPM available can
    go negative when reservations overshoot) would render "-12%". The clamp is
    what keeps a pathological ratio from producing an arbitrarily long cell.

    Over-quota gets its own treatment: the fill SATURATES at `width`, so a
    250%-used provider draws a completely full bar — pixel-identical to one
    sitting exactly at its limit. The leading OVER_QUOTA_MARK is what makes the
    two distinguishable without reading the label. (`load_level` reports the
    same fact as data so the renderer can also colour it.)
    """
    if maximum <= 0:
        return f"{'░' * width} 0%"
    clamped_pct = int(max(0, min(999, current / maximum * 100)))
    filled = max(0, min(width, int(clamped_pct / 100 * width)))
    bar = f"{'█' * filled}{'░' * (width - filled)}"
    mark = OVER_QUOTA_MARK if current > maximum else ""
    return f"{mark}{bar} {clamped_pct}%"


# Semantic load levels, returned as PLAIN DATA so this module stays free of
# Textual and Rich imports (that purity is what makes it unit-testable).
# `proxy_tui.py` maps the level onto a style when it renders the cell.
LOAD_OK = "ok"
LOAD_WARM = "warm"
LOAD_HIGH = "high"
LOAD_OVER = "over"

# Breakpoints for `load_level`, as percent-of-limit:
#   70  "getting warm" — the sliding-window limiter is close enough to its
#       ceiling that bursts start queueing; worth noticing before it alarms.
#   90  the alarm breakpoint. It is exactly the threshold
#       `_check_quota_thresholds` already uses for the alert badge, so the bar
#       and the badge can never disagree about what counts as a warning.
#  >100  over-quota — the limit has been exceeded, not merely approached.
LOAD_WARM_THRESHOLD = 70
LOAD_HIGH_THRESHOLD = 90


def load_level(current: int, maximum: int) -> str:
    """Classify a used/limit pair into a load level (plain string, no styling).

    Non-positive `maximum` and non-positive `current` are LOAD_OK: there is no
    meaningful pressure to report, and a negative value is an artefact of TPM
    reservations overshooting rather than real usage.
    """
    if maximum <= 0 or current <= 0:
        return LOAD_OK
    pct = current / maximum * 100
    if pct > 100:
        return LOAD_OVER
    if pct >= LOAD_HIGH_THRESHOLD:
        return LOAD_HIGH
    if pct >= LOAD_WARM_THRESHOLD:
        return LOAD_WARM
    return LOAD_OK


# Canonical 3-state circuit vocabulary for every render site (grid cell,
# detail-pane title, detail-pane Circuit row). All lowercase, max 4 chars, so
# the words fit the Overview grid's `Circuit` column without truncation.
CIRCUIT_OK = "ok"
CIRCUIT_WARN = "warn"
CIRCUIT_OPEN = "open"


def circuit_glyph(provider_status: dict) -> str:
    """Map status dict keys onto the 3-state circuit label.

    Single source of truth for circuit state in the TUI — every render site
    calls this instead of re-branching on the raw keys.

    `status()` exposes only `circuit_open` + `circuit_failure_count`, never the
    limiter's genuine HALF_OPEN state, so the vocabulary stays honest about
    what it can actually observe:
    breaker tripped -> "open"; closed but with recorded failures -> "warn"
    (deliberately NOT named "half": the status dict cannot express HALF_OPEN);
    otherwise "ok".
    """
    if provider_status.get("circuit_open"):
        return CIRCUIT_OPEN
    if provider_status.get("circuit_failure_count", 0) > 0:
        return CIRCUIT_WARN
    return CIRCUIT_OK


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
            rpm_level = LOAD_OK
            tpm_level = LOAD_OK
        else:
            rpm_cell = mini_bar(rpm_current, p.get("rpm_limit", 0), GRID_BAR_WIDTH)
            # Both cells mean "used / limit". TPM available can go negative
            # when reservations overshoot, so clamp used to a non-negative
            # value; the full-quota case renders a full bar (like RPM).
            used_tpm = max(0, p.get("tpm_limit", 0) - p.get("tpm_available", 0))
            tpm_cell = mini_bar(used_tpm, p.get("tpm_limit", 0), GRID_BAR_WIDTH)
            # Levels are reported as data; `proxy_tui.py` owns the styling.
            rpm_level = load_level(rpm_current, p.get("rpm_limit", 0))
            tpm_level = load_level(used_tpm, p.get("tpm_limit", 0))
        rows.append({
            "key": key,
            "label": PROVIDER_LABELS.get(key, key),
            "circuit": circuit_glyph(p),
            "rpm": rpm_cell,
            "tpm": tpm_cell,
            "rpm_level": rpm_level,
            "tpm_level": tpm_level,
            "forwarded": _compact_number(forwarded),
            # Compacted like `forwarded`: a raw str(n) overflows the narrow
            # 429 column once a provider accumulates five digits.
            "count_429s": _compact_number(n429),
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
