"""Test all three Agnes AI modalities against the running proxy."""
import json
import urllib.request
import urllib.error
import sys

PROXY = "http://127.0.0.1:8899"

def test_text():
    """Test agnes-3.0-flash via POST /v1/chat/completions"""
    print("=" * 60)
    print("TEST 1: Agnes Text (agnes-3.0-flash)")
    print("POST /v1/chat/completions")
    print("=" * 60)
    body = json.dumps({
        "model": "agnes-3.0-flash",
        "messages": [{"role": "user", "content": "Say hello in one word"}],
        "max_tokens": 10,
    }).encode()
    req = urllib.request.Request(
        f"{PROXY}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            data = json.loads(resp.read().decode())
            print(f"Status: {status}")
            print(f"Response: {json.dumps(data, indent=2)[:2000]}")
            # Validate structure
            assert "choices" in data, "Missing 'choices' in response"
            assert len(data["choices"]) > 0, "Empty choices"
            content = data["choices"][0]["message"]["content"]
            print(f"Content: {content!r}")
            print("RESULT: PASS")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP Error: {e.code}")
        print(f"Body: {body[:2000]}")
        print("RESULT: FAIL")
        return False
    except Exception as e:
        print(f"Error: {e}")
        print("RESULT: FAIL")
        return False

def test_image():
    """Test agnes-image-2.1-flash via POST /v1/images/generations"""
    print("\n" + "=" * 60)
    print("TEST 2: Agnes Image (agnes-image-2.1-flash)")
    print("POST /v1/images/generations")
    print("=" * 60)
    body = json.dumps({
        "model": "agnes-image-2.1-flash",
        "prompt": "a red apple on a wooden table",
        "n": 1,
        "size": "1024x1024",
        "response_format": "url",
    }).encode()
    req = urllib.request.Request(
        f"{PROXY}/v1/images/generations",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status = resp.status
            data = json.loads(resp.read().decode())
            print(f"Status: {status}")
            # Don't dump the whole thing - images can be huge
            keys = list(data.keys())
            print(f"Response keys: {keys}")
            if "data" in data:
                print(f"Number of images: {len(data['data'])}")
                for i, img in enumerate(data["data"]):
                    img_keys = list(img.keys())
                    print(f"  Image {i} keys: {img_keys}")
                    if "url" in img:
                        print(f"  URL: {img['url'][:200]}...")
                    elif "b64_json" in img:
                        print(f"  b64_json length: {len(img['b64_json'])}")
            print("RESULT: PASS")
            return True
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"HTTP Error: {e.code}")
        print(f"Body: {body_text[:2000]}")
        print("RESULT: FAIL")
        return False
    except Exception as e:
        print(f"Error: {e}")
        print("RESULT: FAIL")
        return False

def test_video():
    """Test agnes-video-2.5-flash via POST /v1/videos"""
    print("\n" + "=" * 60)
    print("TEST 3: Agnes Video (agnes-video-2.5-flash)")
    print("POST /v1/videos")
    print("=" * 60)
    body = json.dumps({
        "model": "agnes-video-2.5-flash",
        "prompt": "a cat walking slowly across a room",
    }).encode()
    req = urllib.request.Request(
        f"{PROXY}/v1/videos",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status = resp.status
            data = json.loads(resp.read().decode())
            print(f"Status: {status}")
            print(f"Response: {json.dumps(data, indent=2)[:2000]}")
            # Video creation should return a task ID
            print("RESULT: PASS")
            return True
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"HTTP Error: {e.code}")
        print(f"Body: {body_text[:2000]}")
        print("RESULT: FAIL")
        return False
    except Exception as e:
        print(f"Error: {e}")
        print("RESULT: FAIL")
        return False

if __name__ == "__main__":
    results = {}
    results["text"] = test_text()
    results["image"] = test_image()
    results["video"] = test_video()
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for modality, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {modality}: {status}")
    
    if all(results.values()):
        print("\nAll tests passed!")
        sys.exit(0)
    else:
        print("\nSome tests failed!")
        sys.exit(1)
