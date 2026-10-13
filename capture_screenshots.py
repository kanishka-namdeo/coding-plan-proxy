#!/usr/bin/env python
"""Capture TUI screenshots for README documentation."""

import argparse
import asyncio
import json
import os
import sys
from collections import deque
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class ScreenshotSourceError(RuntimeError):
    """The configured base URL does not serve this proxy's status payload."""


# Sentinel for "key absent", so a present-but-None `rate_limits` is not
# confused with a missing one.
_MISSING = object()


class LiveRateLimiterProxy:
    """Rate limiter that fetches status from the running proxy."""

    def __init__(self, base_url: str = "http://127.0.0.1:8899"):
        self.base_url = base_url
        self._recent_latencies: list[float] = []
        # Endpoint unreachable → render the built-in placeholder.
        self.fetch_failed = False
        # Endpoint answered but the payload is not usable → hard error.
        self.payload_error: str | None = None

    def status(self) -> dict:
        """Fetch status from the running proxy's /v1/proxy/status endpoint.

        `GET /v1/proxy/status` does NOT serve the rate limiter's status dict
        directly. `handlers.py` wraps it one level deep as
        `{"rate_limits": rate_limiter.status(), "providers": …,
        "model_overlaps": …}`, while the TUI consumes the limiter status FLAT
        (`primary` / `secondary` / … at the top level — that is what
        `tui_status.provider_grid_rows`, `ProxyTUI._REQUIRED_STATUS_KEYS` and
        `_validate_status` all read). Handing the wrapper straight through left
        the TUI looking one level too deep, so every panel rendered empty.

        So `_normalize_payload` unwraps `rate_limits` here. That is also why a
        `rate_limits`/`providers`/`model_overlaps` payload must NOT be reported
        as a foreign service squatting the port: that IS this proxy's own,
        legitimate response shape.

        Falls back to `_default_status()` only when the endpoint is genuinely
        unreachable. When the endpoint answers but its payload is unusable,
        `payload_error` is set instead: the caller must not render a screenshot
        from a payload it cannot read.
        """
        self.fetch_failed = False
        self.payload_error = None
        try:
            with urlopen(f"{self.base_url}/v1/proxy/status", timeout=5) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            self.fetch_failed = True
            return self._default_status()
        return self._normalize_payload(data)

    def _normalize_payload(self, data) -> dict:
        """Unwrap the HTTP `rate_limits` envelope into the flat limiter status.

        Three outcomes, each recorded on `payload_error` / `fetch_failed` so
        the caller can tell them apart:

        * envelope present and a dict → unwrapped, with `recent_latencies`
          preserved (see `_merge_latencies`);
        * `rate_limits` absent → the payload is already flat, used as-is;
        * anything else (non-object JSON, non-dict `rate_limits`) → unusable.
          That is a shape problem, so it is reported as one — never as an
          `AttributeError` from deep inside a `dict.get` chain.
        """
        if not isinstance(data, dict):
            self.payload_error = (
                f"top-level JSON is a {type(data).__name__}, expected an object"
            )
            return self._default_status()

        wrapped = data.get("rate_limits", _MISSING)
        if wrapped is _MISSING:
            # Already the limiter status (e.g. a hand-rolled mock endpoint).
            limiter = dict(data)
        elif isinstance(wrapped, dict):
            limiter = dict(wrapped)
        else:
            self.payload_error = (
                f"'rate_limits' is a JSON {type(wrapped).__name__}, expected an "
                f"object (top-level keys: {self._key_summary(data)})"
            )
            return self._default_status()

        limiter["recent_latencies"] = self._merge_latencies(limiter, data)
        return limiter

    def _merge_latencies(self, limiter: dict, payload: dict) -> list:
        """Keep the MOST COMPLETE `recent_latencies` the payload can offer.

        The limiter status carries its own combined `recent_latencies`; the HTTP
        envelope may carry one at the top level as well. Neither may be dropped
        for the other, and a populated list must never be blanked by a
        sibling that happens to be empty — so the longer list wins, and
        `self._recent_latencies` remains the last-resort fallback when the
        payload offers neither.
        """
        best: list | None = None
        for candidate in (limiter.get("recent_latencies"), payload.get("recent_latencies")):
            if isinstance(candidate, list) and (best is None or len(candidate) > len(best)):
                best = list(candidate)
        return best if best else list(self._recent_latencies)

    @staticmethod
    def _key_summary(data: dict) -> str:
        keys = sorted(str(k) for k in data)
        return ", ".join(keys[:6]) if keys else "none"

    def _default_status(self) -> dict:
        return {
            "rps_limit": 0, "rpm_limit": 0, "rpm_current": 0,
            "tpm_limit": 0, "tpm_available": 0, "tpm_reserved": 0,
            "total_forwarded": 0, "queue_drops": 0, "queue_p50_ms": 0, "queue_p95_ms": 0, "queue_p99_ms": 0, "total_429s": 0,
            "total_rejected": 0, "total_tokens_consumed": 0,
            "pending_requests": 0, "recent_latencies": [],
            "model_usage": {}, "uptime_seconds": 0,
            "circuit_open": False, "circuit_failure_count": 0,
        }


class MockLogHandler:
    """Log handler that provides sample log entries for screenshots."""

    def __init__(self):
        self.buffer: list[dict] = []
        self._seq = 0

    def add_entry(self, level: str, message: str):
        self._seq += 1
        self.buffer.append({
            "seq": self._seq - 1,
            "level": level,
            "message": message,
            "timestamp": "2026-06-01T18:00:00",
        })

    def get_logs(self, limit: int = 50, from_seq: int = 0):
        return [e for e in self.buffer if e["seq"] >= from_seq][:limit]

    def clear(self):
        self.buffer.clear()
        self._seq = 0


class MockProxyApp(dict):
    """Mock proxy app for TUI integration."""

    def __init__(self):
        super().__init__()
        self["client_session"] = type("Session", (), {"closed": False})()


async def main(base_url: str):
    from proxy_tui import ProxyTUI
    from tui_status import provider_grid_rows

    print(f"Connecting to running proxy at {base_url}...")
    rate_limiter = LiveRateLimiterProxy(base_url)

    # Fail loudly instead of writing a misleading empty dashboard, but name
    # the ACTUAL problem. Three cases, deliberately distinct:
    #
    #   (a) unreachable       → warn, render the built-in placeholder (what
    #                           `_default_status()` exists for), exit 0;
    #   (b) reachable, payload unusable → hard error, NOTHING written;
    #   (c) reachable, payload usable → capture.
    #
    # Case (b) used to blame a foreign service squatting the port. That was
    # wrong: `rate_limits` / `providers` / `model_overlaps` is THIS proxy's own
    # `/v1/proxy/status` envelope, and the cause was the missing unwrap that
    # `LiveRateLimiterProxy._normalize_payload` now performs.
    try:
        probe = rate_limiter.status()
    except Exception as exc:  # pragma: no cover - defensive
        raise ScreenshotSourceError(f"could not read {base_url}/v1/proxy/status: {exc}") from exc
    if rate_limiter.payload_error:
        raise ScreenshotSourceError(
            f"{base_url}/v1/proxy/status answered with a payload shape this TUI "
            f"cannot use: {rate_limiter.payload_error}. Nothing was written. "
            f"Pass --base-url to point at a running DashScope proxy."
        )
    if rate_limiter.fetch_failed:
        print("  WARNING: proxy unreachable — rendering the built-in placeholder status")
    elif not provider_grid_rows(probe):
        raise ScreenshotSourceError(
            f"{base_url}/v1/proxy/status returned a status dict with no provider "
            f"entries, so the Overview grid would be empty "
            f"(top-level keys: {LiveRateLimiterProxy._key_summary(probe)}). "
            f"Nothing was written."
        )

    # Create a log handler with sample entries
    log_handler = MockLogHandler()
    sample_logs = [
        ("INFO", "Proxy started on 127.0.0.1:8899"),
        ("INFO", "Rate limits configured: RPM=12, TPM=2000000"),
        ("INFO", "Forwarding request to upstream DashScope API"),
        ("INFO", "Request completed successfully (model: qwen-max)"),
        ("INFO", "Tokens consumed: 1250 (prompt: 800, completion: 450)"),
        ("WARNING", "Rate limit approaching: RPM usage at 85%"),
        ("INFO", "Request queued (queue depth: 3)"),
        ("INFO", "Retrying request after 429 (attempt 2/40)"),
        ("INFO", "Backoff delay: 2.3s before next retry"),
        ("ERROR", "Upstream timeout after 120s for request req-abc123"),
        ("INFO", "Circuit breaker closed (0 consecutive failures)"),
        ("INFO", "Session log written to session_logs/2026-06-01.jsonl"),
        ("INFO", "TPM token reconciliation: reserved=5000, actual=4200, refunded=800"),
        ("WARNING", "Monthly quota at 75% (67500/90000 requests)"),
        ("INFO", "Client disconnected during streaming response"),
        ("INFO", "Developer role message converted to system role"),
    ]
    for level, msg in sample_logs:
        log_handler.add_entry(level, msg)

    proxy_app = MockProxyApp()

    screenshot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)

    tabs = [
        ("tab-overview", "overview"),
        ("tab-metrics", "metrics"),
        ("tab-logs", "logs"),
        ("tab-models", "models"),
        ("tab-config", "config"),
    ]

    tui_app = ProxyTUI(
        rate_limiter=rate_limiter,
        tui_log_handler=log_handler,
        proxy_app=proxy_app,
    )

    async with tui_app.run_test(size=(120, 40)) as pilot:
        # Let the TUI initialize and poll the running proxy
        await pilot.pause()
        await pilot.pause()

        for tab_id, basename in tabs:
            print(f"Switching to {tab_id}...")
            tui_app.switch_to(tab_id)
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            # Save SVG (for local preview)
            filepath = tui_app.save_screenshot(filename=f"{basename}.svg", path=screenshot_dir)
            print(f"Saved {filepath}")
            # Save PNG via PIL from the SVG
            filepath_png = tui_app.save_screenshot(filename=f"{basename}.png", path=screenshot_dir)
            print(f"Saved {filepath_png}")

    print(f"All screenshots saved to {screenshot_dir}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8899",
        help="base URL of the RUNNING DashScope proxy (default: %(default)s)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args.base_url))
    except ScreenshotSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
