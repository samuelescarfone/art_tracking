"""JSON persistence: artists.json (the registry) and state.json (scrape cache + match decisions)."""

import json
import re
import unicodedata
from datetime import date

from .storage import get_store

# Store-relative paths (see storage.py).
REGISTRY_PATH = "artists.json"
STATE_PATH = "state.json"
ARTIFACTS_DIR = "artifacts/artists"
DROPPED_DIR = "artifacts/dropped"
PREFERENCES_PATH = "artifacts/user_preference/preferences.md"


def name_key(name: str) -> str:
    """Normalize a name for exact matching: 'Ana María Hernando' -> 'ana maria hernando'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"\([^)]*\)", " ", s)          # drop "(Resource Artist)" etc.
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s.lower())
    return " ".join(s.split())


def slugify(name: str) -> str:
    return name_key(name).replace(" ", "-")


def _read(path: str, default):
    text = get_store().read(path)
    return json.loads(text) if text is not None else default


def _write(path: str, data) -> None:
    get_store().write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


class Registry:
    """artists.json - the compact index Claude uses to decide create vs update."""

    def __init__(self):
        data = _read(REGISTRY_PATH, {"updated": None, "artists": []})
        self.artists: dict[str, dict] = {a["slug"]: a for a in data["artists"]}

    def save(self) -> None:
        artists = sorted(self.artists.values(), key=lambda a: a["slug"])
        _write(REGISTRY_PATH, {"updated": date.today().isoformat(), "artists": artists})

    def get(self, slug: str) -> dict | None:
        return self.artists.get(slug)

    def upsert(self, entry: dict) -> None:
        self.artists[entry["slug"]] = entry

    def key_index(self) -> dict[str, str]:
        """name_key of every name and alias -> slug."""
        index = {}
        for a in self.artists.values():
            for n in [a["name"], *a.get("aliases", [])]:
                index[name_key(n)] = a["slug"]
        return index

    def find(self, name: str) -> dict | None:
        slug = self.key_index().get(name_key(name))
        return self.artists.get(slug) if slug else None

    def compact(self) -> list[dict]:
        """The trimmed view sent to Claude: just enough to disambiguate names."""
        return [
            {k: a.get(k) for k in ("slug", "name", "aliases", "one_liner", "affiliations")}
            for a in sorted(self.artists.values(), key=lambda a: a["slug"])
        ]


class State:
    """state.json - per-source page hashes/mentions and cached match decisions."""

    def __init__(self):
        self.data = _read(STATE_PATH, {"sources": {}, "decisions": {}, "last_run": None})
        self.data.setdefault("curation", {})

    def save(self) -> None:
        _write(STATE_PATH, self.data)

    @property
    def sources(self) -> dict:
        return self.data["sources"]

    @property
    def decisions(self) -> dict:
        return self.data["decisions"]

    @property
    def curation(self) -> dict:
        """slug -> curator verdict, valid only while preferences_hash matches."""
        return self.data["curation"]
