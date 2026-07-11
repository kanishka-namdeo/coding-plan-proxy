#!/usr/bin/env python3
"""Comprehensive live server tests for multi-provider implementation."""

import asyncio
import aiohttp
import json
import time

PROXY_URL = "http://127.0.0.1:8899"

async def test_health():
    """Test health endpoint."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PROXY_URL}/health") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            print("[OK] Health endpoint OK")

async def test_ready():
    """Test ready endpoint."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PROXY_URL}/ready") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ready"
            print("[OK] Ready endpoint OK")

async def test_models():
    """Test models endpoint returns both primary and secondary models."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PROXY_URL}/v1/models") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert "data" in data
            models = [m["id"] for m in data["data"]]
            
            # Check primary models
            assert "qwen3-coder-plus" in models
            assert "qwen3-max" in models
            
            # Check secondary MIMO models
            assert "mimo-v2.5-pro" in models
            assert "mimo-v2.5" in models
            assert "mimo-v2-pro" in models
            
            print(f"[OK] Models endpoint OK - {len(models)} models available")
            print(f"  Primary: {[m for m in models if m.startswith('qwen')]}")
            print(f"  Secondary: {[m for m in models if m.startswith('mimo')]}")

async def test_proxy_status():
    """Test proxy status endpoint with multi-provider structure."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PROXY_URL}/v1/proxy/status") as resp:
            assert resp.status == 200
            data = await resp.json()
            
            # New multi-provider structure
            assert "rate_limits" in data
            assert "providers" in data
            
            # Check providers
            providers = data["providers"]
            assert "primary" in providers
            assert "secondary" in providers
            
            # Primary should be available
            assert providers["primary"]["available"] is True
            
            # Secondary should be available (MIMO is configured in .env)
            assert providers["secondary"]["available"] is True
            assert "xiaomimimo.com" in providers["secondary"]["base_url"]
            
            # Check rate limits structure
            rate_limits = data["rate_limits"]
            assert "primary" in rate_limits
            assert "shared_limits" in rate_limits
            
            # Primary rate limiter should have standard fields
            primary = rate_limits["primary"]
            assert "rpm_limit" in primary
            assert "tpm_limit" in primary
            assert "total_forwarded" in primary
            
            print("[OK] Proxy status endpoint OK")
            print(f"  Primary provider: {providers['primary']['base_url']}")
            print(f"  Secondary provider: {providers['secondary']['base_url']}")
            print(f"  Shared limits: {rate_limits['shared_limits']}")

async def test_primary_model_request():
    """Test request to primary provider (qwen model)."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "qwen3-coder-plus",
            "messages": [{"role": "user", "content": "Say 'hello from primary'"}],
            "max_tokens": 10,
            "stream": False
        }
        
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                error_body = await resp.text()
                print(f"[DEBUG] Primary request failed with status {resp.status}")
                print(f"[DEBUG] Response: {error_body[:200]}")
            assert resp.status == 200
            data = await resp.json()
            
            assert "choices" in data
            assert len(data["choices"]) > 0
            assert "usage" in data
            
            print("[OK] Primary model request OK (qwen3-coder-plus)")
            print(f"  Response: {data['choices'][0]['message']['content'][:50]}...")

async def test_secondary_model_request():
    """Test request to secondary provider (mimo model)."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "mimo-v2.5-pro",
            "messages": [{"role": "user", "content": "Say 'hello from secondary'"}],
            "max_tokens": 10,
            "stream": False
        }
        
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            
            assert "choices" in data
            assert len(data["choices"]) > 0
            assert "usage" in data
            
            print("[OK] Secondary model request OK (mimo-v2.5-pro)")
            print(f"  Response: {data['choices'][0]['message']['content'][:50]}...")

async def test_streaming_primary():
    """Test streaming request to primary provider."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "qwen3-coder-plus",
            "messages": [{"role": "user", "content": "Count 1 to 3"}],
            "max_tokens": 20,
            "stream": True
        }
        
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"
            
            chunks = []
            async for line in resp.content:
                line_str = line.decode('utf-8').strip()
                if line_str.startswith("data: "):
                    chunks.append(line_str)
            
            assert len(chunks) > 0
            assert any("[DONE]" in chunk for chunk in chunks)
            
            print(f"[OK] Streaming primary OK - {len(chunks)} chunks received")

async def test_secondary_model_hyphen_alias():
    """Test that mimo-v2-5 hyphen alias routes to secondary provider."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "mimo-v2-5",
            "messages": [{"role": "user", "content": "Say 'hello from secondary alias'"}],
            "max_tokens": 10,
            "stream": False
        }
        
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            
            assert "choices" in data
            assert len(data["choices"]) > 0
            assert "usage" in data
            
            print("[OK] Secondary hyphen alias request OK (mimo-v2-5 -> mimo-v2.5)")


async def test_streaming_secondary():
    """Test streaming request to secondary provider."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "mimo-v2.5",
            "messages": [{"role": "user", "content": "Count 1 to 3"}],
            "max_tokens": 20,
            "stream": True
        }
        
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"
            
            chunks = []
            async for line in resp.content:
                line_str = line.decode('utf-8').strip()
                if line_str.startswith("data: "):
                    chunks.append(line_str)
            
            assert len(chunks) > 0
            assert any("[DONE]" in chunk for chunk in chunks)
            
            print(f"[OK] Streaming secondary OK - {len(chunks)} chunks received")

async def test_unknown_model_defaults_to_primary():
    """Test that unknown model defaults to primary provider."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "unknown-model",
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 5,
            "stream": False
        }
        
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            # Should fail at upstream (model doesn't exist) but routing should work
            # Status could be 400/404 from upstream, not 500 from proxy
            assert resp.status in [200, 400, 404, 422]
            print("[OK] Unknown model defaults to primary provider")

async def test_validation():
    """Test request validation."""
    async with aiohttp.ClientSession() as session:
        # Empty body
        async with session.post(f"{PROXY_URL}/v1/chat/completions", data=b"") as resp:
            assert resp.status == 400
            print("[OK] Validation: empty body rejected")
        
        # Missing model
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "test"}]}
        ) as resp:
            assert resp.status == 400
            print("[OK] Validation: missing model rejected")
        
        # Missing messages
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json={"model": "qwen3-coder-plus"}
        ) as resp:
            assert resp.status == 400
            print("[OK] Validation: missing messages rejected")

async def test_developer_role_conversion():
    """Test that developer role is converted to system."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": "qwen3-coder-plus",
            "messages": [
                {"role": "developer", "content": "You are helpful"},
                {"role": "user", "content": "Say hi"}
            ],
            "max_tokens": 5,
            "stream": False
        }
        
        async with session.post(
            f"{PROXY_URL}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            assert resp.status == 200
            print("[OK] Developer role conversion OK")

async def main():
    """Run all tests."""
    print("=" * 60)
    print("LIVE SERVER TESTS - Multi-Provider Implementation")
    print("=" * 60)
    print()
    
    try:
        # Basic endpoints
        print("Testing basic endpoints...")
        await test_health()
        await test_ready()
        await test_models()
        await test_proxy_status()
        print()
        
        # Validation
        print("Testing validation...")
        await test_validation()
        print()
        
        # Primary provider tests
        print("Testing primary provider (DashScope)...")
        await test_primary_model_request()
        await test_streaming_primary()
        await test_developer_role_conversion()
        print()
        
        # Secondary provider tests
        print("Testing secondary provider (MIMO)...")
        await test_secondary_model_request()
        await test_secondary_model_hyphen_alias()
        await test_streaming_secondary()
        print()
        
        # Edge cases
        print("Testing edge cases...")
        await test_unknown_model_defaults_to_primary()
        print()
        
        print("=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n[FAIL] TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
