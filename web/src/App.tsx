import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  getArtist,
  listArtists,
  refreshArtist,
  UnauthorizedError,
  type Artist,
  type ArtistSummary,
} from "./api";

const TOKEN_KEY = "art-tracker-admin-token";
const STAGE_LABEL: Record<string, string> = {
  starting: "Starting",
  researching: "Researching",
  writing: "Writing profile",
  saving: "Saving",
};

function slugFromPath(): string | null {
  const m = window.location.pathname.match(/^\/a\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

function readToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function formatDate(iso: string): string {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, "0");
  return `${m}:${s}`;
}

export default function App() {
  const [artists, setArtists] = useState<ArtistSummary[] | null>(null);
  const [listError, setListError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(slugFromPath());

  useEffect(() => {
    listArtists()
      .then((list) => {
        setArtists(list);
        setSelected((cur) => cur ?? list[0]?.slug ?? null);
      })
      .catch((e) => setListError(String(e.message ?? e)));
    const onPop = () => setSelected(slugFromPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const select = (slug: string) => {
    if (slug === selected) return;
    window.history.pushState(null, "", `/a/${slug}`);
    setSelected(slug);
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!artists) return [];
    return q ? artists.filter((a) => a.name.toLowerCase().includes(q)) : artists;
  }, [artists, query]);

  const onRefreshed = useCallback((artist: Artist) => {
    setArtists((list) =>
      list ? list.map((a) => (a.slug === artist.slug ? { ...a, ...artist } : a)) : list,
    );
  }, []);

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="wordmark">
          Watchlist
          <span>Denver</span>
        </div>
        <input
          className="search"
          type="search"
          placeholder="Search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search artists"
        />
        <nav className="artist-list" aria-label="Artists">
          {listError && <p className="muted small">Couldn't load artists: {listError}</p>}
          {artists === null && !listError && <p className="muted small">Loading…</p>}
          {artists?.length === 0 && <p className="muted small">No artists yet. Run main.py.</p>}
          {filtered.map((a) => (
            <button
              key={a.slug}
              className={`artist-link${a.slug === selected ? " active" : ""}`}
              onClick={() => select(a.slug)}
            >
              {a.name}
            </button>
          ))}
        </nav>
        {artists && artists.length > 0 && (
          <div className="sidebar-foot muted small">
            {artists.length} {artists.length === 1 ? "artist" : "artists"}
          </div>
        )}
      </aside>

      <main className="main">
        {selected ? (
          <ArtistView key={selected} slug={selected} onRefreshed={onRefreshed} />
        ) : (
          <div className="empty">
            <p className="display muted">Select an artist</p>
          </div>
        )}
      </main>
    </div>
  );
}

function ArtistView({ slug, onRefreshed }: { slug: string; onRefreshed: (a: Artist) => void }) {
  const [artist, setArtist] = useState<Artist | null>(null);
  const [error, setError] = useState("");
  const [stage, setStage] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [notice, setNotice] = useState("");
  const [needToken, setNeedToken] = useState(false);
  const [tokenDraft, setTokenDraft] = useState("");
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    getArtist(slug)
      .then(setArtist)
      .catch((e) => setError(String(e.message ?? e)));
    return () => window.clearInterval(timer.current);
  }, [slug]);

  const runRefresh = async (token: string) => {
    setNotice("");
    setNeedToken(false);
    setStage("starting");
    setElapsed(0);
    const started = Date.now();
    timer.current = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    try {
      await refreshArtist(slug, token, (e) => {
        if (e.stage === "done") {
          setArtist(e.artist);
          onRefreshed(e.artist);
          setNotice(`Updated · $${e.cost_usd.toFixed(2)}`);
        } else if (e.stage === "error") {
          setNotice(`Refresh failed: ${e.message}`);
        } else if (e.stage !== "heartbeat") {
          setStage(e.stage);
        }
      });
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        try {
          localStorage.removeItem(TOKEN_KEY);
        } catch {
          /* storage unavailable */
        }
        setNeedToken(true);
      } else {
        setNotice(`Refresh failed: ${(e as Error).message}`);
      }
    } finally {
      window.clearInterval(timer.current);
      setStage(null);
    }
  };

  const saveTokenAndRefresh = (e: React.FormEvent) => {
    e.preventDefault();
    try {
      localStorage.setItem(TOKEN_KEY, tokenDraft);
    } catch {
      /* storage unavailable: token used for this attempt only */
    }
    runRefresh(tokenDraft);
  };

  if (error) return <div className="empty"><p className="muted">Couldn't load profile: {error}</p></div>;
  if (!artist) return <div className="empty"><p className="muted small">Loading…</p></div>;

  // The profile body starts with "# Name", which the header already shows.
  const body = artist.markdown.replace(/^#\s[^\n]*\n+/, "");
  const busy = stage !== null;

  return (
    <article className="artist">
      {busy && <div className="progress" aria-hidden />}

      <div className="toolbar">
        <span className="status small muted" aria-live="polite">
          {busy ? `${STAGE_LABEL[stage] ?? stage} · ${formatElapsed(elapsed)}` : notice}
        </span>
        {needToken ? (
          <form className="token-form" onSubmit={saveTokenAndRefresh}>
            <input
              type="password"
              placeholder="Admin token"
              value={tokenDraft}
              onChange={(e) => setTokenDraft(e.target.value)}
              autoFocus
            />
            <button className="btn" type="submit" disabled={!tokenDraft}>
              Refresh
            </button>
          </form>
        ) : (
          <button className="btn" onClick={() => runRefresh(readToken())} disabled={busy}>
            <span className={`icon${busy ? " spin" : ""}`} aria-hidden>
              ↻
            </span>
            {busy ? "Refreshing" : "Refresh"}
          </button>
        )}
      </div>

      <header className="hero">
        <p className="eyebrow">
          <span className={`dot ${artist.momentum}`} />
          {artist.momentum}
          {artist.medium?.length > 0 && <> · {artist.medium.slice(0, 3).join(", ")}</>}
        </p>
        <h1 className="display">{artist.name}</h1>
        {artist.one_liner && <p className="lede">{artist.one_liner}</p>}

        <dl className="facts">
          {artist.based_in && (
            <div>
              <dt>Based in</dt>
              <dd>{artist.based_in}</dd>
            </div>
          )}
          {artist.affiliations?.length > 0 && (
            <div>
              <dt>Found via</dt>
              <dd>{artist.affiliations.join(", ")}</dd>
            </div>
          )}
          <div>
            <dt>Updated</dt>
            <dd>{formatDate(artist.last_updated)}</dd>
          </div>
          {(artist.website || artist.instagram) && (
            <div>
              <dt>Links</dt>
              <dd className="links">
                {artist.website && (
                  <a href={artist.website} target="_blank" rel="noreferrer">
                    Website
                  </a>
                )}
                {artist.instagram && (
                  <a
                    href={`https://instagram.com/${artist.instagram.replace(/^@/, "")}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Instagram
                  </a>
                )}
              </dd>
            </div>
          )}
        </dl>
      </header>

      <div className="prose">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
          }}
        >
          {body}
        </ReactMarkdown>
      </div>

      {artist.changelog.length > 0 && (
        <footer className="changelog">
          <h2>Changelog</h2>
          <ul>
            {[...artist.changelog].reverse().map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </footer>
      )}
    </article>
  );
}
