"""Where tracker data lives: local files during development, a private Vercel Blob store when deployed.

Paths are always repo-relative POSIX strings, e.g. "artists.json" or "artifacts/artists/jane-doe.md".
Selection: STORAGE=blob|local; defaults to blob on Vercel, local everywhere else.
"""

import os
import tempfile
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LocalStore:
    kind = "local"

    def __init__(self, root: Path = ROOT):
        self.root = root

    def read(self, path: str) -> str | None:
        p = self.root / path
        return p.read_text(encoding="utf-8") if p.exists() else None

    def write(self, path: str, text: str) -> None:
        """Atomic write so a crash mid-run never leaves a half-written file."""
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)

    def delete(self, path: str) -> None:
        (self.root / path).unlink(missing_ok=True)

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())


class BlobStore:
    """Private Vercel Blob store. Needs BLOB_READ_WRITE_TOKEN (or Vercel OIDC + BLOB_STORE_ID)."""

    kind = "blob"

    def __init__(self):
        from vercel.blob import sync as blob

        self.blob = blob

    def read(self, path: str) -> str | None:
        try:
            return self.blob.get_blob(path, access="private").body.decode("utf-8")
        except self.blob.BlobNotFoundError:
            return None

    def write(self, path: str, text: str) -> None:
        content_type = "text/markdown" if path.endswith(".md") else "application/json"
        self.blob.put_blob(path, text.encode("utf-8"), access="private", content_type=content_type,
                           allow_overwrite=True, cache_control_max_age=60)

    def delete(self, path: str) -> None:
        try:
            self.blob.delete_blob(path)
        except self.blob.BlobNotFoundError:
            pass

    def list(self, prefix: str) -> list[str]:
        return sorted(b.pathname for b in self.blob.query_blobs(prefix=prefix))


def make_store(kind: str):
    return BlobStore() if kind == "blob" else LocalStore()


@lru_cache(maxsize=1)
def get_store():
    kind = os.environ.get("STORAGE") or ("blob" if os.environ.get("VERCEL") else "local")
    return make_store(kind)


SYNCED_FILES = ["artists.json", "state.json"]
SYNCED_PREFIXES = ["artifacts/"]


def sync(src, dst, log=print) -> int:
    """Copy all tracker data from one store to another (used by --push / --pull)."""
    paths = [p for p in SYNCED_FILES if src.read(p) is not None]
    for prefix in SYNCED_PREFIXES:
        paths += src.list(prefix)
    for path in paths:
        dst.write(path, src.read(path))
        log(f"  {path}")
    return len(paths)
