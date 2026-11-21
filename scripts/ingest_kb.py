#!/usr/bin/env python3
"""Embed kb/salon-faq.md into the kb_chunk table with Ollama bge-m3.

One `##` section = one chunk, keyed by a slug derived from its heading, so editing a section
updates it in place rather than leaving a stale duplicate the agent might still retrieve.

    python3 scripts/ingest_kb.py           # upsert every section
    python3 scripts/ingest_kb.py --prune   # also delete chunks whose section is gone

The whole file is embedded and written in ONE transaction: a half-applied knowledge base is
worse than an out-of-date one, because the agent answers confidently from whatever survived.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "kb" / "salon-faq.md"
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
CONTAINER = os.environ.get("BOOKING_PG_CONTAINER", "booking-pg-local")
DB = ["-U", os.environ.get("PGUSER", "n8n"), "-d", os.environ.get("PGDATABASE", "salon_booking")]
PRUNE = "--prune" in sys.argv


def sections(md: str) -> list[tuple[str, str, str]]:
    """(slug, heading, body) for each `## ` section. The intro above the first one is notes."""
    out = []
    for block in re.split(r"^## ", md, flags=re.M)[1:]:
        heading, _, body = block.partition("\n")
        heading, body = heading.strip(), body.strip()
        if not body:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
        # The heading rides along in the embedded text: "Parking" is the whole signal for
        # "where do I park", and a body that never repeats the word would miss it.
        out.append((slug, heading, f"{heading}\n{body}"))
    return out


def embed(text: str) -> list[float]:
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings",
        data=json.dumps({"model": MODEL, "prompt": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        v = json.loads(r.read().decode())["embedding"]
    if len(v) != 1024:
        raise SystemExit(f"{MODEL} returned {len(v)} dims, expected 1024 — kb_chunk is halfvec(1024)")
    return v


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"missing {SOURCE}")
    chunks = sections(SOURCE.read_text())
    if not chunks:
        raise SystemExit("no ## sections found — refusing to wipe the knowledge base")

    stmts = ["BEGIN;"]
    for slug, heading, text in chunks:
        vec = embed(text)
        stmts.append(
            "INSERT INTO kb_chunk (slug, heading, content, embedding) VALUES ("
            f"{lit(slug)}, {lit(heading)}, {lit(text)}, '[{','.join(f'{x:.6f}' for x in vec)}]'::halfvec(1024)) "
            "ON CONFLICT (slug) DO UPDATE SET heading = EXCLUDED.heading, content = EXCLUDED.content, "
            "embedding = EXCLUDED.embedding, updated_at = now();"
        )
        print(f"  embedded  {slug}")
    if PRUNE:
        keep = ",".join(lit(s) for s, _, _ in chunks)
        stmts.append(f"DELETE FROM kb_chunk WHERE slug NOT IN ({keep});")
    stmts.append("COMMIT;")

    out = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", *DB, "-q", "-v", "ON_ERROR_STOP=1"],
        input="\n".join(stmts), capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SystemExit("ingest failed, nothing written: " + out.stderr.strip())

    total = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", *DB, "-Atc", "select count(*) from kb_chunk"],
        capture_output=True, text=True,
    ).stdout.strip()
    print(f"{len(chunks)} section(s) ingested; kb_chunk now holds {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
