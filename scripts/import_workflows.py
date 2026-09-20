#!/usr/bin/env python3
"""Push every workflow in ./workflows into a local n8n, creating or updating by name.

The n8n MCP tooling refuses localhost ("Localhost access is blocked in strict mode"), so the
public REST API is the way in. Needs an API key — n8n Settings → n8n API → create one:

    N8N_API_KEY=... python3 scripts/import_workflows.py
    N8N_API_KEY=... python3 scripts/import_workflows.py --activate

Sub-workflows called through Execute Workflow must be ACTIVE in this n8n version, so
--activate is what you want on a fresh import.

Why two passes
--------------
A workflow that calls another one needs the callee's *id*, and n8n mints a fresh id per
instance — so a committed id is only ever correct on the machine that exported it. The
committed JSON therefore carries `@@WF:<workflow name>@@` wherever an id belongs, and the
second pass swaps each placeholder for the id this n8n just assigned. Names are stable and
reviewable in a diff; ids are not.

Credential ids inside the JSON are local to the n8n that exported them: after importing into
a different instance, open each Postgres node once and pick your own credential.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("N8N_BASE_URL", "http://localhost:5678").rstrip("/")
KEY = os.environ.get("N8N_API_KEY", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTIVATE = "--activate" in sys.argv
PLACEHOLDER = re.compile(r"@@WF:([^@]+)@@")

# The harness is a webhook that can call any workflow by name. It is a development tool and
# must never be switched on by a bulk import — see README "Known limits".
NEVER_ACTIVATE = ("90 · ",)


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

    files = sorted((ROOT / "workflows").glob("*.json"))
    wanted = {json.loads(p.read_text())["name"] for p in files}

    # Fail before touching anything if a placeholder names a workflow we are not importing —
    # a typo here would otherwise import cleanly and then fail at the first call.
    for p in files:
        for name in PLACEHOLDER.findall(p.read_text()):
            if name not in wanted:
                raise SystemExit(f"{p.name}: @@WF:{name}@@ names no workflow in ./workflows")

    live = {w["name"]: w["id"] for w in api("GET", "/workflows?limit=250")["data"]}

    # Pass 1 — make sure every workflow EXISTS, so every id is known. New ones are created empty:
    # n8n refuses to save a workflow that references a sub-workflow it cannot resolve, so the
    # content cannot go in until all the ids do.
    for path in files:
        name = json.loads(path.read_text())["name"]
        if name not in live:
            live[name] = api("POST", "/workflows",
                             {"name": name, "nodes": [], "connections": {}, "settings": {}})["id"]
            print(f"created  {live[name]}  {name}")

    # Pass 2 — now every @@WF:name@@ can be resolved, so push the real content.
    for path in files:
        raw = path.read_text()
        wf = json.loads(PLACEHOLDER.sub(lambda m: live[m.group(1)], raw))
        api("PUT", f"/workflows/{live[wf['name']]}",
            {k: wf[k] for k in ("name", "nodes", "connections", "settings")})
        refs = len(PLACEHOLDER.findall(raw))
        print(f"pushed   {live[wf['name']]}  {wf['name']}"
              + (f"  ({refs} reference(s) resolved)" if refs else ""))

    if ACTIVATE:
        for path in files:
            name = json.loads(path.read_text())["name"]
            if name.startswith(NEVER_ACTIVATE):
                print(f"SKIPPED  activation of {name} — dev-only webhook, activate it by hand")
                continue
            try:
                api("POST", f"/workflows/{live[name]}/activate")
            except SystemExit as e:
                # A workflow whose credential you have not picked yet (the Telegram adapter,
                # until you add a bot token) must not abort the import of everything else.
                print(f"not activated: {name} — {str(e).splitlines()[-1].strip()[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
