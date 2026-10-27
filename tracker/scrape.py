"""Collect raw artist mentions from every enabled source in sources.yaml."""

import hashlib
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .llm import Claude
from .state import State

USER_AGENT = "DenverArtTracker/1.0 (personal research; +https://github.com)"
MAX_PAGE_CHARS = 100_000

EXTRACT_SYSTEM = (
    "You extract artist names from the visible text of an art-organization web page. "
    "Return only real individual artists or artist collectives. Exclude staff, board members, "
    "curators, sponsors, funders, navigation labels and program names. Normalize capitalization, "
    "and strip role annotations like '(Resource Artist)' from the name but keep them in `context`."
)
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "artists": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "context": {"type": "string", "description": "e.g. cohort years or role on this page"},
                },
                "required": ["name", "context"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["artists"],
    "additionalProperties": False,
}


def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def _slug_to_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w)


def _mention(source: dict, name: str, url: str, context: str = "") -> dict:
    return {
        "name": " ".join(name.split()),
        "source_id": source["id"],
        "source_name": source["name"],
        "tier": source["tier"],
        "url": url,
        "context": context or source.get("context", ""),
    }


def parse_sitemap(source: dict, raw: str) -> list[dict]:
    pattern = re.compile(source["pattern"])
    out = []
    for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", raw):
        m = pattern.search(loc)
        if m:
            # Slug-derived names are rough ("Chris Antemann K Moholt"); the match step cleans them up.
            out.append(_mention(source, _slug_to_name(m.group(1)), loc, "name derived from URL slug"))
    return out


def parse_links(source: dict, raw: str) -> list[dict]:
    pattern = re.compile(source["pattern"])
    soup = BeautifulSoup(raw, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        name = a.get_text(" ", strip=True)
        if not name or not pattern.search(a["href"]) or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(_mention(source, name, urljoin(source["url"], a["href"])))
    return out


def visible_text(raw: str) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "form"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def parse_page_text(source: dict, raw: str, claude: Claude) -> list[dict]:
    text = visible_text(raw)
    if len(text) > MAX_PAGE_CHARS:
        raise ValueError(f"page text is {len(text):,} chars (> {MAX_PAGE_CHARS:,}); narrow the URL")
    prompt = (
        f"Source: {source['name']}\nURL: {source['url']}\n"
        f"What this page lists: {source.get('context', 'artists')}\n\n<page_text>\n{text}\n</page_text>"
    )
    result = claude.json_call("extract", EXTRACT_SYSTEM, prompt, EXTRACT_SCHEMA, effort="low")
    return [_mention(source, a["name"], source["url"], a["context"]) for a in result["artists"]]


def collect(sources: list[dict], state: State, claude: Claude, delay: float, log) -> tuple[list[dict], list[str]]:
    """Return (mentions, errors). Unchanged pages reuse cached mentions, so no Claude call is spent."""
    mentions, errors = [], []
    for source in sources:
        if not source.get("enabled", True) or not source.get("url"):
            continue
        sid = source["id"]
        cached = state.sources.get(sid, {})
        try:
            raw = fetch(source["url"])
            time.sleep(delay)
        except requests.RequestException as e:
            errors.append(f"{sid}: fetch failed ({e})")
            if cached.get("mentions"):
                log(f"  {source['name']}: fetch failed, using {len(cached['mentions'])} cached mentions")
                mentions.extend(cached["mentions"])
            continue

        content = visible_text(raw) if source["kind"] == "page_text" else raw
        digest = hashlib.sha256(content.encode()).hexdigest()
        if cached.get("hash") == digest and "mentions" in cached:
            found = cached["mentions"]
            log(f"  {source['name']}: unchanged, {len(found)} artists")
        else:
            try:
                if source["kind"] == "sitemap":
                    found = parse_sitemap(source, raw)
                elif source["kind"] == "links":
                    found = parse_links(source, raw)
                elif source["kind"] == "page_text":
                    found = parse_page_text(source, raw, claude)
                else:
                    raise ValueError(f"unknown kind {source['kind']!r}")
            except Exception as e:  # one bad source shouldn't sink the run
                errors.append(f"{sid}: parse failed ({e})")
                found = cached.get("mentions", [])
            else:
                if not found:
                    errors.append(f"{sid}: 0 artists parsed - site layout may have changed")
                    found = cached.get("mentions", [])
                else:
                    state.sources[sid] = {
                        "hash": digest,
                        "mentions": found,
                        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
            log(f"  {source['name']}: {len(found)} artists")
        mentions.extend(found)
    return mentions, errors
