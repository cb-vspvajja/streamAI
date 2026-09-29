"""Small live probes for the selected endpoints; read settings from exported env."""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request


def setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or any(marker in value.upper() for marker in ("CHANGE_ME", "YOUR_", "YOUR-")):
        raise ValueError(f"Set {name} before running this check")
    return value


def request(prefix: str, route: str, body: dict):
    root = setting(f"{prefix}_BASE_URL").rstrip("/").removesuffix("/v1")
    if not root.startswith("https://") and root != os.environ.get("MODEL_USAGE_URL", "") + "/setup":
        raise ValueError(f"{prefix}_BASE_URL must use https:// for this Capella check")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + setting(f"{prefix}_API_KEY")}
    extra = os.environ.get(f"{prefix}_HEADERS_JSON", "")
    if extra:
        headers.update(json.loads(extra))
    if prefix == "CHAT":
        headers["X-cb-cache"] = "none"
    req = urllib.request.Request(root + "/v1/" + route, data=json.dumps(body).encode(), headers=headers, method="POST")
    # Credentials are never printed. Normal certificate verification stays enabled.
    return urllib.request.urlopen(req, timeout=60)


def main() -> None:
    expected = int(setting("EMBEDDING_DIMENSIONS"))
    model = setting("EMBEDDING_MODEL")
    for mode in ("query", "passage"):
        body = {"model": model, "input": "A family-friendly comedy for tonight.", "input_type": mode, "encoding_format": "float"}
        with request("EMBEDDING", "embeddings", body) as response:
            vector = json.load(response)["data"][0]["embedding"]
        if not isinstance(vector, list) or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in vector):
            raise ValueError(f"Embedding {mode}: response must contain a finite numeric array")
        if len(vector) != expected:
            raise ValueError(f"Embedding {mode}: returned {len(vector)} dimensions; configured {expected}")
        print(f"PASS: embedding {mode}: {len(vector)} dimensions")

    body = {"model": setting("CHAT_MODEL"), "messages": [{"role": "system", "content": "You are a concise assistant."}, {"role": "user", "content": "Reply with the word READY."}], "stream": True, "temperature": 0.1, "max_tokens": 32}
    content = []
    with request("CHAT", "chat/completions", body) as response:
        for raw in response:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            if event.get("error"):
                raise ValueError("Chat endpoint returned an error in the stream")
            choices = event.get("choices") or []
            if choices:
                content.append(choices[0].get("delta", {}).get("content") or "")
    if not "".join(content).strip():
        raise ValueError("Chat request returned no streamed text")
    print("PASS: chat model returned streamed text")
    print("Model endpoint checks passed. Next verify Search and Agent Memory separately.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        print(f"FAIL: model endpoint HTTP {exc.code}. Check the selected model, endpoint, key and API parameters.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Exceptions can contain an endpoint, but never include our request headers.
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
