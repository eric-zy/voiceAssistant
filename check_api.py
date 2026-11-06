"""Test authentication with GET /models. No chat generation, no credential output."""
import argparse
import getpass
import os
from urllib.parse import urlsplit

import httpx
from api_config import normalize_key, fingerprint


def check(key, base_url, model, *, transport=None):
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        print("Invalid API URL: use an HTTPS base URL without embedded credentials.")
        return 1
    print(f"API host: {parsed.hostname}")
    print(f"Key fingerprint (SHA256): {fingerprint(key)}")
    if parsed.hostname != "api.deepseek.com":
        print("This is NOT DeepSeek's official host. The key must belong to this API provider.")
    if not key or any(char.isspace() for char in key) or not key.isascii():
        print("Invalid key format: empty, internal whitespace or non-ASCII characters.")
        return 1
    try:
        with httpx.Client(timeout=20, transport=transport, follow_redirects=False) as client:
            response = client.get(base_url.rstrip("/") + "/models", headers={"Authorization":"Bearer " + key})
        print(f"GET /models: HTTP {response.status_code}")
        if response.status_code != 200:
            messages = {
                401: "Authentication rejected. This is not an ASR or chat-model-name problem. Check the key/provider pairing.",
                402: "Insufficient account balance.",
                403: "Access forbidden by the API or an intermediate gateway.",
                404: "Model-list endpoint not found. Check the base URL.",
                407: "Proxy authentication required.",
                429: "Rate limited. Retry later.",
            }
            print(messages.get(response.status_code, "API verification failed. Check the provider's service status."))
            return 1
        models = {item.get("id") for item in response.json().get("data", [])}
        print("Authentication OK.")
        if model not in models:
            print(f"Configured model {model!r} was not listed. Set DEEPSEEK_MODEL to a model available for your account.")
            print("Available models:", ", ".join(sorted(x for x in models if isinstance(x, str))))
            return 1
        print("Configured model is listed. Chat generation has not been tested.")
        return 0
    except (httpx.HTTPError, ValueError):
        # Never print raw exceptions, response bodies or headers which may contain credentials.
        print("Network/TLS/proxy failure or unexpected response format. No key or server response was printed.")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="store_true", help="Use DEEPSEEK_API_KEY instead of prompting")
    args = parser.parse_args()
    raw_key = os.getenv("DEEPSEEK_API_KEY", "") if args.env else getpass.getpass("DeepSeek API Key (hidden): ")
    raise SystemExit(check(normalize_key(raw_key), os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                           os.getenv("DEEPSEEK_MODEL", "deepseek-flash")))
