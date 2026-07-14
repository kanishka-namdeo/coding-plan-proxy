#!/usr/bin/env python3
"""Real-world tests against all three providers through the running proxy."""

import asyncio
import aiohttp
import json
import time

PROXY_URL = "http://127.0.0.1:8899"

# Test models for each provider
PRIMARY_MODEL = "qwen3-coder-plus"
SECONDARY_MODEL = "mimo-v2.5-pro"
TERTIARY_MODEL = "kat-coder-pro-v2.5"

results = []


async def test_provider(name, model, stream=False):
    """Test a single provider with a real API call."""
    url = f"{PROXY_URL}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say 'hello' in one word."}],
        "max_tokens": 20,
        "stream": stream,
    }
    headers = {"Content-Type": "application/json"}

    start = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                elapsed = time.monotonic() - start
                status = resp.status

                if stream:
                    # Read entire response body, then parse SSE
                    body_bytes = await resp.read()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    
                    # Parse SSE: split by lines, extract data: fields
                    chunks = []
                    for line in body_text.split("\n"):
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            chunks.append(json.loads(data_str))
                        except json.JSONDecodeError:
                            pass
                    
                    if status == 200 and chunks:
                        content = chunks[0].get("choices", [{}])[0].get("delta", {}).get("content", "")
                        results.append(("PASS", name, f"stream={stream}", f"status={status}", f"chunks={len(chunks)}", f"content={content[:30]!r}", f"latency={elapsed:.2f}s"))
                    else:
                        results.append(("FAIL", name, f"stream={stream}", f"status={status}", f"chunks={len(chunks)}", f"latency={elapsed:.2f}s", body_text[:200]))
                else:
                    data = await resp.json()
                    if status == 200 and "choices" in data:
                        content = data["choices"][0]["message"]["content"]
                        usage = data.get("usage", {})
                        results.append(("PASS", name, f"stream={stream}", f"status={status}", f"content={content[:30]!r}", f"tokens={usage.get('total_tokens', '?')}", f"latency={elapsed:.2f}s"))
                    else:
                        results.append(("FAIL", name, f"stream={stream}", f"status={status}", f"latency={elapsed:.2f}s", json.dumps(data)[:200]))
    except Exception as e:
        elapsed = time.monotonic() - start
        results.append(("FAIL", name, f"stream={stream}", f"error={e}", f"latency={elapsed:.2f}s"))


async def test_models_endpoint():
    """Test that /v1/models returns models from all providers."""
    url = f"{PROXY_URL}/v1/models"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                models = [m["id"] for m in data.get("data", [])]
                has_primary = any("qwen" in m for m in models)
                has_secondary = any("mimo" in m for m in models)
                has_tertiary = any("kat" in m for m in models)
                if has_primary and has_secondary and has_tertiary:
                    results.append(("PASS", "models_endpoint", f"count={len(models)}", f"primary={has_primary}", f"secondary={has_secondary}", f"tertiary={has_tertiary}"))
                else:
                    results.append(("FAIL", "models_endpoint", f"primary={has_primary}", f"secondary={has_secondary}", f"tertiary={has_tertiary}", f"models={models}"))
    except Exception as e:
        results.append(("FAIL", "models_endpoint", f"error={e}"))


async def test_status_endpoint():
    """Test /v1/proxy/status returns multi-provider info."""
    url = f"{PROXY_URL}/v1/proxy/status"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                has_rate_limits = "rate_limits" in data
                has_providers = "providers" in data
                if has_rate_limits and has_providers:
                    providers = data["providers"]
                    provider_names = list(providers.keys())
                    results.append(("PASS", "status_endpoint", f"providers={provider_names}"))
                else:
                    results.append(("FAIL", "status_endpoint", f"keys={list(data.keys())}"))
    except Exception as e:
        results.append(("FAIL", "status_endpoint", f"error={e}"))


async def main():
    print("=" * 70)
    print("REAL-WORLD PROVIDER TESTS")
    print("=" * 70)
    print()

    # Infrastructure tests
    print("[1/8] Testing /v1/models endpoint...")
    await test_models_endpoint()

    print("[2/8] Testing /v1/proxy/status endpoint...")
    await test_status_endpoint()

    # Primary provider tests
    print("[3/8] Testing PRIMARY provider (non-streaming)...")
    await test_provider("PRIMARY", PRIMARY_MODEL, stream=False)

    print("[4/8] Testing PRIMARY provider (streaming)...")
    await test_provider("PRIMARY", PRIMARY_MODEL, stream=True)

    # Secondary provider tests
    print("[5/8] Testing SECONDARY provider (non-streaming)...")
    await test_provider("SECONDARY", SECONDARY_MODEL, stream=False)

    print("[6/8] Testing SECONDARY provider (streaming)...")
    await test_provider("SECONDARY", SECONDARY_MODEL, stream=True)

    # Tertiary provider tests
    print("[7/8] Testing TERTIARY provider (non-streaming)...")
    await test_provider("TERTIARY", TERTIARY_MODEL, stream=False)

    print("[8/8] Testing TERTIARY provider (streaming)...")
    await test_provider("TERTIARY", TERTIARY_MODEL, stream=True)

    # Print results
    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print()

    passes = sum(1 for r in results if r[0] == "PASS")
    fails = sum(1 for r in results if r[0] == "FAIL")

    for r in results:
        status = r[0]
        icon = "✓" if status == "PASS" else "✗"
        detail = " | ".join(r[1:])
        print(f"  {icon} [{status}] {detail}")

    print()
    print(f"Total: {passes} passed, {fails} failed out of {len(results)} tests")
    print()

    if fails == 0:
        print("ALL TESTS PASSED - All three providers are working correctly!")
    else:
        print(f"WARNING: {fails} test(s) failed - check details above")

    return 0 if fails == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
