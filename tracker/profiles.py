"""Read/write artifacts/<slug>.md and keep artists.json in sync with each write."""

from datetime import date

import yaml

from .state import ARTIFACTS_DIR, DROPPED_DIR, Registry
from .storage import get_store

CHANGELOG_HEADING = "## Changelog"


def path_for(slug: str) -> str:
    return f"{ARTIFACTS_DIR}/{slug}.md"


def dropped_path_for(slug: str) -> str:
    return f"{DROPPED_DIR}/{slug}.md"


def split(md: str) -> tuple[dict, str, list[str]]:
    """Return (front matter, body, changelog lines)."""
    meta, rest = {}, md
    if md.startswith("---\n"):
        end = md.find("\n---\n", 4)
        if end != -1:
            meta = yaml.safe_load(md[4:end]) or {}
            rest = md[end + 5:]
    body, _, log = rest.partition(f"\n{CHANGELOG_HEADING}\n")
    lines = [ln for ln in log.splitlines() if ln.startswith("- ")]
    return meta, body.strip(), lines


def load(slug: str) -> tuple[dict, str, list[str]] | None:
    store = get_store()
    for p in (path_for(slug), dropped_path_for(slug)):
        text = store.read(p)
        if text is not None:
            return split(text)
    return None


def save(registry: Registry, item: dict, profile: dict, existing: tuple | None) -> dict:
    """Write the markdown file and upsert its registry entry. Returns the entry."""
    today = date.today().isoformat()
    slug = item["slug"]
    old = registry.get(slug) or {}
    old_meta, _, changelog = existing or ({}, "", [])

    # Sources and affiliations accumulate across runs; nothing is dropped automatically.
    sources = {(s["source_id"], s["url"]): s for s in old.get("sources", [])}
    for m in item["mentions"]:
        sources.setdefault((m["source_id"], m["url"]), {
            "source_id": m["source_id"], "source_name": m["source_name"],
            "tier": m["tier"], "url": m["url"], "context": m["context"], "first_seen": today,
        })
    affiliations = sorted({s["source_name"] for s in sources.values()})
    aliases = sorted({*old.get("aliases", []), *item["aliases"], *profile["aliases"]} - {profile["name"]})
    first_seen = old.get("first_seen") or old_meta.get("first_seen") or today

    meta = {
        "name": profile["name"],
        "slug": slug,
        "affiliations": affiliations,
        "medium": profile["medium"],
        "based_in": profile["based_in"],
        "website": profile["website"],
        "instagram": profile["instagram"],
        "momentum": profile["momentum"],
        "first_seen": first_seen,
        "last_updated": today,
    }
    changelog = [*changelog, f"- {today}: {profile['changelog_entry']}"]
    md = (
        "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n"
        + profile["body_markdown"].strip() + "\n\n"
        + CHANGELOG_HEADING + "\n" + "\n".join(changelog) + "\n"
    )
    store = get_store()
    store.write(path_for(slug), md)
    if old.get("status") == "dropped":
        store.delete(dropped_path_for(slug))  # re-added after a preferences change

    entry = {
        "slug": slug,
        "name": profile["name"],
        "aliases": aliases,
        "one_liner": profile["one_liner"],
        "affiliations": affiliations,
        "momentum": profile["momentum"],
        "medium": profile["medium"],
        "based_in": profile["based_in"],
        "priority": item["priority"],
        "first_seen": first_seen,
        "last_updated": today,
        "status": "active",
        "file": path_for(slug),
        "sources": sorted(sources.values(), key=lambda s: (s["source_id"], s["url"])),
    }
    registry.upsert(entry)
    registry.save()
    return entry


def rebuild_registry(registry: Registry) -> int:
    """Regenerate artists.json entries from profile front matter (keeps one_liner/sources if present)."""
    count = 0
    store = get_store()
    for p in store.list(ARTIFACTS_DIR + "/"):
        if not p.endswith(".md"):
            continue
        meta, _, _ = split(store.read(p))
        if not meta.get("name"):
            continue
        slug = meta.get("slug") or p.rsplit("/", 1)[-1][:-3]
        old = registry.get(slug) or {}
        registry.upsert({
            **old,
            "slug": slug,
            "name": meta["name"],
            "aliases": old.get("aliases", []),
            "one_liner": old.get("one_liner", ""),
            "affiliations": meta.get("affiliations", old.get("affiliations", [])),
            "momentum": meta.get("momentum", "unclear"),
            "medium": meta.get("medium", old.get("medium", [])),
            "based_in": meta.get("based_in", old.get("based_in", "")),
            "priority": old.get("priority", 3),
            "first_seen": str(meta.get("first_seen", date.today().isoformat())),
            "last_updated": str(meta.get("last_updated", date.today().isoformat())),
            "status": old.get("status", "active"),
            "file": p,
            "sources": old.get("sources", []),
        })
        count += 1
    registry.save()
    return count


def drop(registry: Registry, slug: str, reason: str) -> None:
    """Curator rejected a tracked artist: stop refreshing it and move its profile to artifacts/dropped/."""
    entry = registry.get(slug)
    if not entry or entry.get("status") == "dropped":
        return
    store = get_store()
    text = store.read(path_for(slug))
    if text is not None:
        store.write(dropped_path_for(slug), text)
        store.delete(path_for(slug))
        entry["file"] = dropped_path_for(slug)
    entry["status"] = "dropped"
    entry["dropped_reason"] = reason
    registry.save()
