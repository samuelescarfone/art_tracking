"""Web API for the art tracker. Deployed on Vercel as the `api` service; run locally with:

    .venv/bin/uvicorn server:app --reload --port 8000
"""

import hmac
import json
import os
import queue
import threading

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from tracker import pipeline, profiles
from tracker.llm import Claude, ClaudeError
from tracker.state import Registry
from tracker.storage import get_store

HEARTBEAT_SECONDS = 10

load_dotenv()  # local dev: picks up ADMIN_TOKEN / CLAUDE_KEY from .env (no-op on Vercel)

app = FastAPI(title="Art Tracker")


def _summary(entry: dict) -> dict:
    keys = ("slug", "name", "one_liner", "momentum", "medium", "based_in", "affiliations",
            "first_seen", "last_updated")
    return {k: entry.get(k) for k in keys}


def _active(slug: str) -> dict:
    entry = Registry().get(slug)
    if not entry or entry.get("status", "active") != "active":
        raise HTTPException(404, "Artist not found")
    return entry


def _check_token(token: str | None) -> None:
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected:
        if os.environ.get("VERCEL"):
            raise HTTPException(503, "Refresh is disabled: set ADMIN_TOKEN in the Vercel project")
        return  # local development
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "Invalid admin token")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "storage": get_store().kind}


@app.get("/api/artists")
def list_artists() -> list[dict]:
    artists = [a for a in Registry().artists.values() if a.get("status", "active") == "active"]
    return [_summary(a) for a in sorted(artists, key=lambda a: a["name"].lower())]


@app.get("/api/artists/{slug}")
def get_artist(slug: str) -> dict:
    entry = _active(slug)
    loaded = profiles.load(slug)
    if not loaded:
        raise HTTPException(404, "Profile file missing")
    meta, body, changelog = loaded
    return {
        **_summary(entry),
        "website": meta.get("website") or "",
        "instagram": meta.get("instagram") or "",
        "markdown": body,
        "changelog": [line[2:] for line in changelog],
    }


@app.post("/api/artists/{slug}/refresh")
def refresh_artist(slug: str, x_admin_token: str | None = Header(default=None)) -> StreamingResponse:
    """Re-research one artist. Streams NDJSON progress lines; the last is `done` or `error`.

    Heartbeats keep the connection alive during the ~1-3 minute research call.
    """
    _check_token(x_admin_token)
    item = pipeline.item_from_entry(_active(slug))
    config = pipeline.load_config()
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            claude = Claude(config["settings"]["models"])
            pipeline.run(item, claude, config, on_stage=lambda s: events.put({"stage": s}))
            events.put({"stage": "done", "artist": get_artist(slug),
                        "cost_usd": round(claude.usage.cost, 3)})
        except (ClaudeError, anthropic.APIError) as e:
            events.put({"stage": "error", "message": str(e)})
        except Exception as e:  # surface anything unexpected to the UI instead of a dead stream
            events.put({"stage": "error", "message": f"{type(e).__name__}: {e}"})

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            try:
                event = events.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                yield json.dumps({"stage": "heartbeat"}) + "\n"
                continue
            yield json.dumps(event, ensure_ascii=False) + "\n"
            if event["stage"] in ("done", "error"):
                return

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache"})
