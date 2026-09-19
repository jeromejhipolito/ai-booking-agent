#!/usr/bin/env python3
"""Push every workflow in ./workflows into a local n8n, creating or updating by name.

The n8n MCP tooling refuses localhost ("Localhost access is blocked in strict mode"), so the
public REST API is the way in. Needs an API key — n8n Settings → n8n API → create one:

    N8N_API_KEY=... python3 scripts/import_workflows.py
    N8N_API_KEY=... python3 scripts/import_workflows.py --activate

Sub-workflows called through Execute Workflow must be ACTIVE in this n8n version, so
--activate is what you want on a fresh import.

Credential ids inside the JSON are local to the n8n that exported them: after importing into
a different instance, open each Postgres node once and pick your own credential.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("N8N_BASE_URL", "http://localhost:5678").rstrip("/")
KEY = os.environ.get("N8N_API_KEY", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTIVATE = "--activate" in sys.argv


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{BASE}/api/v1{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")


def main() -> int:
    if not KEY:
        raise SystemExit("set N8N_API_KEY (n8n Settings -> n8n API -> create an API key)")

    existing = {w["name"]: w["id"] for w in api("GET", "/workflows?limit=250")["data"]}

    for path in sorted((ROOT / "workflows").glob("*.json")):
        wf = json.loads(path.read_text())
        payload = {k: wf[k] for k in ("name", "nodes", "connections", "settings")}
        wid = existing.get(wf["name"])
        if wid:
            api("PUT", f"/workflows/{wid}", payload)
            action = "updated"
        else:
            wid = api("POST", "/workflows", payload)["id"]
            action = "created"
        if ACTIVATE:
            api("POST", f"/workflows/{wid}/activate")
        print(f"{action:8} {wid}  {wf['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
