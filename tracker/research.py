"""Per-artist Claude calls: web research, then a structured profile rewrite."""

import json
from datetime import date

from .llm import Claude

RESEARCH_SYSTEM = """\
You are an art-market researcher tracking up-and-coming artists connected to Denver galleries,
residencies and MFA programs. Use web search and web fetch to gather current, verifiable facts.

Look for: the artist's own website and Instagram; gallery representation; current, upcoming and
recent exhibitions (solo vs group, venue, dates); residencies, grants, awards; press (e.g. Westword,
Denver Post, Colorado Public Radio, Artforum, Hyperallergic); art-fair appearances; museum
acquisitions or shows; public prices or sell-through when visible; education.

Be careful with identity: many artists share names. Only report facts you are confident belong to
this artist, and say so when something is uncertain. Every fact needs a source URL.

When an existing profile is provided, focus on what is NEW or CHANGED since its last update, and
flag anything in it that now looks wrong or outdated.

Finish with plain research notes (no preamble): bullet points grouped by topic, each ending with
its source URL, then a short "Signals" paragraph assessing career momentum."""

WRITE_SYSTEM = """\
You write and maintain markdown profiles of artists for a personal art-collecting tracker.
Given the existing profile body (if any) and new research notes, produce the updated profile.

Rules:
- Merge, don't discard: keep facts from the existing profile that are still valid, add new ones,
  and move finished exhibitions from current/upcoming to past.
- Every factual claim must be supported by a source in the Sources section; keep existing sources.
- Never invent facts. If research found little, say so briefly rather than padding.
- `body_markdown` must start with "# <Name>" and contain exactly these sections in order:
  ## Summary, ## Practice & Style, ## Exhibitions (with ### Current & Upcoming and ### Past),
  ## Press & Recognition, ## Market Signals, ## Why Watch, ## Sources.
  Do NOT include front matter or a Changelog section; those are added separately.
- `one_liner`: under 140 characters - who they are, medium, the strongest Denver connection. It is
  used to tell this artist apart from others with similar names.
- `momentum`: emerging (early career, first notable shows), rising (accelerating: notable
  representation, fairs, press, museum interest), established (long, mature career), or unclear.
- `changelog_entry`: one sentence describing what changed in this update."""

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "medium": {"type": "array", "items": {"type": "string"}},
        "based_in": {"type": "string"},
        "website": {"type": "string"},
        "instagram": {"type": "string"},
        "momentum": {"type": "string", "enum": ["emerging", "rising", "established", "unclear"]},
        "one_liner": {"type": "string"},
        "body_markdown": {"type": "string"},
        "changelog_entry": {"type": "string"},
    },
    "required": ["name", "aliases", "medium", "based_in", "website", "instagram",
                 "momentum", "one_liner", "body_markdown", "changelog_entry"],
    "additionalProperties": False,
}


def _mentions_block(mentions: list[dict]) -> str:
    rows = [{"source": m["source_name"], "tier": m["tier"], "url": m["url"], "context": m["context"]}
            for m in mentions]
    return json.dumps(rows, ensure_ascii=False, indent=1)


def research(claude: Claude, item: dict, existing_body: str | None, signals: dict, settings: dict) -> str:
    signal_lines = "\n".join(f"- {s}" for s in signals.get("validators", []) + signals.get("national", []))
    parts = [
        f"Artist: {item['name']}",
        f"Also listed as: {', '.join(item['aliases'])}" if item["aliases"] else "",
        f"Today: {date.today().isoformat()}",
        f"\nWhere the tracker found them (fetch these pages first):\n{_mentions_block(item['mentions'])}",
        f"\nNewly seen this run:\n{_mentions_block(item['new_mentions'])}" if item["new_mentions"] else "",
        f"\nAlso check for these career signals:\n{signal_lines}",
        (f"\nExisting profile (last updated {item['last_updated']}):\n<profile>\n{existing_body}\n</profile>"
         if existing_body else "\nThere is no existing profile; this is a new artist."),
    ]
    return claude.research_call(
        "research",
        RESEARCH_SYSTEM,
        "\n".join(p for p in parts if p),
        max_searches=settings["max_web_searches_per_artist"],
        max_fetches=settings["max_web_fetches_per_artist"],
    )


def write(claude: Claude, item: dict, existing_body: str | None, notes: str) -> dict:
    prompt = "\n".join([
        f"Artist: {item['name']}",
        f"Today: {date.today().isoformat()}",
        f"<existing_profile>\n{existing_body}\n</existing_profile>" if existing_body
        else "<existing_profile>none - create a new profile</existing_profile>",
        f"<research_notes>\n{notes}\n</research_notes>",
    ])
    return claude.json_call("write", WRITE_SYSTEM, prompt, WRITE_SCHEMA, effort="low", max_tokens=32000)
