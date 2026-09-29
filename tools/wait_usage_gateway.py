import os
import json
import time
import urllib.request

for attempt in range(30):
    try:
        with urllib.request.urlopen(os.environ["MODEL_USAGE_URL"]+"/health", timeout=6) as response:
            assert response.status == 200
            state = json.load(response)
            assert state.get("healthy") and state.get("storage") == "couchbase"
            assert state.get("ledgerId") == os.getenv("USAGE_LEDGER_ID", "streamai")
        print("Couchbase-backed measured-usage gateway is ready")
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)
