"""Research + write + save for one artist. Shared by the CLI (main.py) and the web API (server.py)."""

from pathlib import Path

import yaml

from . import profiles, research
from .llm import Claude, ClaudeError
from .state import Registry

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with (ROOT / "sources.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def item_from_entry(entry: dict) -> dict:
    """A work item for an already-tracked artist, using the sources saved in its registry entry."""
    return {
        "slug": entry["slug"],
        "name": entry["name"],
        "action": "update",
        "priority": entry.get("priority", 3),
        "aliases": entry.get("aliases", []),
        "mentions": entry.get("sources", []),
        "new_mentions": [],
        "stale": True,
        "last_updated": entry.get("last_updated", ""),
    }


def run(item: dict, claude: Claude, config: dict, on_stage=lambda stage: None) -> dict:
    """Research the artist, rewrite the profile, save it. Returns the new registry entry."""
    existing = profiles.load(item["slug"])
    body = existing[1] if existing else None
    on_stage("researching")
    notes = research.research(claude, item, body, config.get("signals", {}), config["settings"])
    if not notes:
        raise ClaudeError("research returned no notes")
    on_stage("writing")
    profile = research.write(claude, item, body, notes)
    on_stage("saving")
    # Reload right before saving so a concurrent save elsewhere isn't overwritten.
    return profiles.save(Registry(), item, profile, existing)
