"""The curator: decides whether an artist belongs on the tracking list, given your preferences."""

import hashlib
import json
from datetime import date

from .llm import Claude, web_tools
from .state import PREFERENCES_PATH
from .storage import get_store

CURATE_SYSTEM = """\
You are the curator for a personal art-collecting tracker. Decide whether an artist should be
added to (or kept on) the tracking list, based strictly on the collector's preferences below.

Work out the artist's main medium and style. If the information provided isn't enough, use web
search (the gallery page and the artist's own site are the best sources). Judge by the artist's
main body of work, not a single piece. Be careful that you are looking at the right person.

Verdict:
- "add": the main body of work fits the preferences.
- "reject": it falls under something the collector excludes, or you cannot tell what they make.

<collector_preferences>
{preferences}
</collector_preferences>"""

CURATE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["add", "reject"]},
        "medium": {"type": "string"},
        "style": {"type": "string"},
        "reason": {"type": "string", "description": "one sentence citing the preference that decided it"},
    },
    "required": ["verdict", "medium", "style", "reason"],
    "additionalProperties": False,
}


def load_preferences() -> tuple[str, str]:
    """Return (text, hash). Cached verdicts are reused only while the hash is unchanged."""
    text = get_store().read(PREFERENCES_PATH)
    if text is None:
        raise SystemExit(f"Missing preferences file: {PREFERENCES_PATH}")
    text = text.strip()
    return text, hashlib.sha256(text.encode()).hexdigest()[:16]


def curate(claude: Claude, item: dict, profile_body: str | None, preferences: str,
           prefs_hash: str, max_searches: int) -> dict:
    found_on = [{"source": m["source_name"], "url": m["url"], "context": m["context"]}
                for m in item["mentions"]]
    parts = [
        f"Artist: {item['name']}",
        f"Also listed as: {', '.join(item['aliases'])}" if item["aliases"] else "",
        f"Found on:\n{json.dumps(found_on, ensure_ascii=False, indent=1)}",
        # An existing profile already describes the work, so no search should be needed.
        f"Existing profile:\n<profile>\n{profile_body[:6000]}\n</profile>" if profile_body else "",
    ]
    verdict = claude.json_call(
        "curate",
        CURATE_SYSTEM.format(preferences=preferences),
        "\n\n".join(p for p in parts if p),
        CURATE_SCHEMA,
        effort="low",
        tools=web_tools(claude.model_for("curate"), max_searches),
    )
    verdict["preferences_hash"] = prefs_hash
    verdict["decided_at"] = date.today().isoformat()
    return verdict
