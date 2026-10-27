"""Turn raw mentions into per-artist work items: exact match first, then Claude decides the rest."""

import json
from datetime import date, datetime, timezone

from .llm import Claude
from .state import Registry, State, name_key, slugify

CHUNK = 120  # unknown names per decision call

DECIDE_SYSTEM = """\
You maintain the registry for a Denver art tracker focused on up-and-coming artists.
You receive (1) the current registry of tracked artists and (2) names newly found on gallery,
residency and MFA web pages that did not exactly match a registry name.

For each mention decide:
- "update": it is the same person/collective as a registry artist (spelling variant, nickname,
  middle name, slug-derived name, duo listed differently). Set `slug` to that registry slug.
- "create": a real artist or collective not in the registry. Set `slug` to "".
- "skip": not an artist (program names, page headings, staff, sponsors, junk).

Also return:
- `canonical_name`: the artist's properly written name. Names derived from URL slugs can be
  mangled ("Angel Ricardo Ricardo Rios", "Chris Antemann K Moholt") - repair them sensibly.
- `priority` 1-5 for how worth tracking they are as an up-and-coming artist:
  5 = early-career / emerging with a Denver or Colorado connection (e.g. a current resident),
  3 = mid-career or unclear, 1 = blue-chip established, an estate, or deceased.
Only use what you can reasonably infer from the name and context; never guess wildly.
Return exactly one decision per mention id."""

DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "update", "skip"]},
                    "slug": {"type": "string"},
                    "canonical_name": {"type": "string"},
                    "priority": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "action", "slug", "canonical_name", "priority", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}

def _group(mentions: list[dict]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for m in mentions:
        key = name_key(m["name"])
        if not key:
            continue
        g = groups.setdefault(key, {"names": set(), "mentions": []})
        g["names"].add(m["name"])
        g["mentions"].append(m)
    return groups

def _decide(unknown: dict[str, dict], registry: Registry, claude: Claude, log) -> dict[str, dict]:
    """Ask Claude about names with no exact registry match. Returns {name_key: decision}."""

    keys = list(unknown)
    out = {}
    registry_json = json.dumps(registry.compact(), ensure_ascii=False)
    for start in range(0, len(keys), CHUNK):
        chunk = keys[start:start + CHUNK]
        items = []
        for i, key in enumerate(chunk):
            g = unknown[key]
            items.append({
                "id": f"m{i}",
                "names": sorted(g["names"]),
                "found_on": [
                    {"source": m["source_name"], "tier": m["tier"], "context": m["context"]}
                    for m in g["mentions"]
                ],
            })
        prompt = (
            f"<registry>\n{registry_json}\n</registry>\n\n"
            f"<mentions>\n{json.dumps(items, ensure_ascii=False)}\n</mentions>"
        )
        log(f"  deciding {len(chunk)} unmatched names with Claude...")
        result = claude.json_call("decide", DECIDE_SYSTEM, prompt, DECIDE_SCHEMA, effort="low")
        by_id = {d["id"]: d for d in result["decisions"]}
        for i, key in enumerate(chunk):
            d = by_id.get(f"m{i}")
            if d is None:
                continue  # left undecided; retried next run
            if d["action"] == "update" and not registry.get(d["slug"]):
                d["action"], d["slug"] = "create", ""  # Claude pointed at a slug that doesn't exist
            d["decided_at"] = date.today().isoformat()
            out[key] = d
    return out

def build_work(mentions: list[dict], registry: Registry, state: State, claude: Claude,
               refresh_after_days: int, log) -> tuple[list[dict], dict]:
    """Return (work items for every tracked or newly found artist, stats)."""
    groups = _group(mentions)
    index = registry.key_index()

    unknown = {k: g for k, g in groups.items() if k not in index and k not in state.decisions}
    if unknown:
        state.decisions.update(_decide(unknown, registry, claude, log))

    items: dict[str, dict] = {}

    def item_for(slug: str, name: str, action: str, priority: int) -> dict:
        if slug not in items:
            items[slug] = {"slug": slug, "name": name, "action": action, "priority": priority,
                           "aliases": set(), "mentions": []}
        return items[slug]

    stats = {"matched": 0, "decided_update": 0, "create": 0, "skip": 0, "undecided": 0}
    for key, g in groups.items():
        if key in index:
            slug = index[key]
            it = item_for(slug, registry.get(slug)["name"], "update", 3)
            stats["matched"] += 1
        else:
            d = state.decisions.get(key)
            if d is None:
                stats["undecided"] += 1
                continue
            if d["action"] == "skip":
                stats["skip"] += 1
                continue
            slug = d["slug"] if d["action"] == "update" else slugify(d["canonical_name"])
            if registry.get(slug):
                it = item_for(slug, registry.get(slug)["name"], "update", d["priority"])
                stats["decided_update"] += 1
            else:
                it = item_for(slug, d["canonical_name"], "create", d["priority"])
                stats["create"] += 1
            it["priority"] = max(it["priority"], d["priority"])
        it["aliases"].update(g["names"])
        it["mentions"].extend(g["mentions"])

    # Tracked artists not seen this run can still be refreshed when stale.
    for slug, entry in registry.artists.items():
        item_for(slug, entry["name"], "update", 2)

    today = datetime.now(timezone.utc).date()
    for it in items.values():
        entry = registry.get(it["slug"])
        known = {(s["source_id"], s["url"]) for s in (entry or {}).get("sources", [])}
        it["new_mentions"] = [m for m in it["mentions"] if (m["source_id"], m["url"]) not in known]
        if entry:
            age = (today - date.fromisoformat(entry["last_updated"])).days
            it["stale"] = age >= refresh_after_days
            it["last_updated"] = entry["last_updated"]
        else:
            it["stale"], it["last_updated"] = True, ""
        it["aliases"] = sorted(it["aliases"])
    return list(items.values()), stats

def select(items: list[dict], limit: int, force: bool) -> list[dict]:
    """Pick what to research this run: new artists and new info first, then oldest stale profiles."""
    todo = [it for it in items if force or it["action"] == "create" or it["new_mentions"] or it["stale"]]
    todo.sort(key=lambda it: (
        not (it["action"] == "create" or it["new_mentions"]),
        -it["priority"],
        it["last_updated"],
    ))
    return todo[:limit] if limit else todo
