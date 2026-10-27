"""Denver art tracker: scrape sources, match against artists.json, curate by preferences, research, write.

Usage:
  python main.py                     # normal run (capped by max_artists_per_run)
  python main.py --dry-run           # scrape, match and curate; show what would be researched
  python main.py --artist "Name"     # research one artist, skipping the curator
  python main.py --limit 25          # override the per-run research cap (0 = no limit)
  python main.py --force             # ignore staleness; re-research everything selected
  python main.py --rebuild-registry  # regenerate artists.json from artifacts/*.md
  python main.py --push | --pull     # sync local data <-> Vercel Blob (needs BLOB_READ_WRITE_TOKEN)

Set STORAGE=blob to run the whole pipeline directly against the deployed Blob store.
"""

import argparse
import sys
from datetime import datetime, timezone

import anthropic

from tracker import curate, match, pipeline, profiles, scrape, storage
from tracker.llm import Claude, ClaudeError
from tracker.state import Registry, State, slugify


def log(msg: str) -> None:
    print(msg, flush=True)


def single_artist_item(name: str, registry: Registry, mentions: list[dict]) -> dict:
    entry = registry.find(name)
    slug = entry["slug"] if entry else slugify(name)
    mine = [m for m in mentions if slugify(m["name"]) == slug]
    known = {(s["source_id"], s["url"]) for s in (entry or {}).get("sources", [])}
    return {
        "slug": slug,
        "name": entry["name"] if entry else name,
        "action": "update" if entry else "create",
        "priority": (entry or {}).get("priority", 3),
        "aliases": [name] if entry and name != entry["name"] else [],
        "mentions": mine,
        "new_mentions": [m for m in mine if (m["source_id"], m["url"]) not in known],
        "stale": True,
        "last_updated": (entry or {}).get("last_updated", ""),
    }


def curate_queue(candidates: list[dict], claude: Claude, registry: Registry, state: State,
                 settings: dict, research_limit: int, log,
                 check_only: set[str] = frozenset()) -> tuple[list[dict], list[str], int]:
    """Walk the queue in priority order, asking the curator until the research budget is full.

    Slugs in `check_only` are tracked artists being re-checked: rejects are dropped, approvals
    stay as they are and are not researched.

    Returns (approved items to research, rejection lines, candidates left unchecked).
    """
    preferences, prefs_hash = curate.load_preferences()
    curation_limit = settings.get("max_curations_per_run", 0)
    approved, rejected, checked = [], [], 0
    for n, item in enumerate(candidates):
        if research_limit and len(approved) >= research_limit:
            return approved, rejected, len(candidates) - n
        slug = item["slug"]
        verdict = state.curation.get(slug)
        fresh = not verdict or verdict["preferences_hash"] != prefs_hash
        if fresh:
            if curation_limit and checked >= curation_limit:
                return approved, rejected, len(candidates) - n
            existing = profiles.load(slug)
            try:
                verdict = curate.curate(claude, item, existing[1] if existing else None, preferences,
                                        prefs_hash, settings["max_web_searches_per_curation"])
            except (ClaudeError, anthropic.APIError) as e:
                log(f"  curator failed on {item['name']}: {e}")
                continue
            state.curation[slug] = verdict
            checked += 1
            mark = "+" if verdict["verdict"] == "add" else "-"
            log(f"  {mark} {item['name']}: {verdict['medium']} | {verdict['reason']}")
        if verdict["verdict"] == "add":
            if slug not in check_only:
                approved.append(item)
        else:
            if registry.get(slug):
                profiles.drop(registry, slug, verdict["reason"])
            if fresh:
                rejected.append(f"{item['name']}: {verdict['reason']}")
    return approved, rejected, 0


def process(item: dict, claude: Claude, config: dict) -> str:
    entry = pipeline.run(item, claude, config)
    return f"{entry['name']} [{entry['momentum']}] - {entry['one_liner']}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--artist")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rebuild-registry", action="store_true")
    ap.add_argument("--push", action="store_true", help="copy local data up to Vercel Blob")
    ap.add_argument("--pull", action="store_true", help="copy Vercel Blob data down to local files")
    args = ap.parse_args()

    if args.push or args.pull:
        local, blob = storage.make_store("local"), storage.make_store("blob")
        src, dst = (local, blob) if args.push else (blob, local)
        log(f"{'Pushing' if args.push else 'Pulling'} tracker data...")
        log(f"Copied {storage.sync(src, dst, log)} files.")
        return 0

    config = pipeline.load_config()
    settings = config["settings"]
    registry = Registry()

    if args.rebuild_registry:
        log(f"Rebuilt registry from {profiles.rebuild_registry(registry)} profiles.")
        return 0

    state = State()
    claude = Claude(settings["models"])
    rejected: list[str] = []

    log("1/4 Collecting artists from sources...")
    mentions, errors = scrape.collect(
        config["sources"], state, claude, settings.get("request_delay_seconds", 1.0), log)
    log(f"  {len(mentions)} mentions total")

    if args.artist:
        # Naming an artist explicitly bypasses the curator.
        todo = [single_artist_item(args.artist, registry, mentions)]
    else:
        log("2/4 Matching against artists.json...")
        items, stats = match.build_work(mentions, registry, state, claude, settings["refresh_after_days"], log)
        log("  " + ", ".join(f"{k}: {v}" for k, v in stats.items()))
        state.save()  # persist scrape cache + decisions so they're never paid for twice

        log("3/4 Curating against your preferences...")
        limit = settings["max_artists_per_run"] if args.limit is None else args.limit
        candidates = match.select(items, 0, args.force)
        # Tracked artists not due for research still get re-checked when preferences change;
        # they're listed first so rejects drop off the list right away.
        due = {it["slug"] for it in candidates}
        recheck = [it for it in items if it["slug"] not in due and registry.get(it["slug"])
                   and registry.get(it["slug"]).get("status") != "dropped"]
        candidates = recheck + candidates
        recheck_slugs = {it["slug"] for it in recheck}
        try:
            todo, rejected, waiting = curate_queue(candidates, claude, registry, state, settings, limit, log,
                                                    check_only=recheck_slugs)
        finally:
            state.save()
        log(f"  {len(todo)} approved for research, {len(rejected)} rejected this run")
        if waiting:
            log(f"  {waiting} candidates still queued for later runs")

    if args.dry_run:
        log("\nDry run - would research:")
        for it in todo:
            why = "new" if it["action"] == "create" else ("new info" if it["new_mentions"] else "stale")
            sources = ", ".join(sorted({m["source_name"] for m in it["mentions"]})) or "-"
            log(f"  [p{it['priority']}] {it['name']} ({why}) <- {sources}")
        log(f"\n{claude.usage.summary()}")
        return 0

    log(f"4/4 Researching {len(todo)} artists...")
    created, updated, failed = [], [], []
    for n, item in enumerate(todo, 1):
        log(f"  [{n}/{len(todo)}] {item['name']} ({item['action']})")
        try:
            line = process(item, claude, config)
        except (ClaudeError, anthropic.APIError) as e:
            failed.append(f"{item['name']}: {e}")
            log(f"    failed: {e}")
            continue
        (created if item["action"] == "create" else updated).append(line)
        log(f"    ok: {line}")

    state.data["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.save()

    log("\n=== Summary ===")
    for label, rows in (("Created", created), ("Updated", updated), ("Rejected by curator", rejected),
                        ("Failed", failed), ("Source problems", errors)):
        if rows:
            log(f"{label} ({len(rows)}):")
            for r in rows:
                log(f"  - {r}")
    log(claude.usage.summary())
    return 1 if failed and not (created or updated) else 0


if __name__ == "__main__":
    sys.exit(main())
