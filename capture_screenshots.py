#!/usr/bin/env python
"""Capture TUI screenshots for README documentation."""

import asyncio
import json
import os
import sys
from collections import deque
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class LiveRateLimiterProxy:
    """Rate limiter that fetches status from the running proxy."""

    def __init__(self, base_url: str = "http://127.0.0.1:8899"):
        self.base_url = base_url
        self._recent_latencies: list[float] = []

    def status(self) -> dict:
        """Fetch status from the running proxy's /v1/proxy/status endpoint."""
        try:
            with urlopen(f"{self.base_url}/v1/proxy/status", timeout=5) as resp:
                data = json.loads(resp.read().decode())
                # Add recent_latencies if not present
                if "recent_latencies" not in data:
                    data["recent_latencies"] = self._recent_latencies
                return data
        except Exception:
            return self._default_status()

    def _default_status(self) -> dict:
        return {
            "rps_limit": 0, "rpm_limit": 0, "rpm_current": 0,
            "tpm_limit": 0, "tpm_available": 0, "tpm_reserved": 0,
            "requests_5h": 0, "requests_5h_limit": 0,
            "requests_week": 0, "requests_week_limit": 0,
            "requests_month": 0, "requests_month_limit": 0,
            "total_forwarded": 0, "total_queued": 0, "total_429s": 0,
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


async def main():
    from proxy_tui import ProxyTUI

    # Connect to the running proxy
    print("Connecting to running proxy at http://127.0.0.1:8899...")
    rate_limiter = LiveRateLimiterProxy("http://127.0.0.1:8899")

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
            filepath = tui_app.save_screenshot(filename=f"{basename}.svg", path=screenshot_dir)
            print(f"Saved {filepath}")

    print(f"All screenshots saved to {screenshot_dir}/")


if __name__ == "__main__":
    asyncio.run(main())
