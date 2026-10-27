export type ArtistSummary = {
  slug: string;
  name: string;
  one_liner: string;
  momentum: "emerging" | "rising" | "established" | "unclear";
  medium: string[];
  based_in: string;
  affiliations: string[];
  first_seen: string;
  last_updated: string;
};

export type Artist = ArtistSummary & {
  website: string;
  instagram: string;
  markdown: string;
  changelog: string[];
};

export type RefreshEvent =
  | { stage: "researching" | "writing" | "saving" | "heartbeat" }
  | { stage: "done"; artist: Artist; cost_usd: number }
  | { stage: "error"; message: string };

export class UnauthorizedError extends Error {}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export const listArtists = () => getJson<ArtistSummary[]>("/api/artists");
export const getArtist = (slug: string) => getJson<Artist>(`/api/artists/${slug}`);

/** POST a refresh and feed each NDJSON progress line to `onEvent` until done/error. */
export async function refreshArtist(
  slug: string,
  token: string,
  onEvent: (e: RefreshEvent) => void,
): Promise<void> {
  const res = await fetch(`/api/artists/${slug}/refresh`, {
    method: "POST",
    headers: token ? { "X-Admin-Token": token } : {},
  });
  if (res.status === 401) throw new UnauthorizedError("Invalid admin token");
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `${res.status} ${res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line) as RefreshEvent;
      if (event.stage === "done" || event.stage === "error") finished = true;
      onEvent(event);
    }
  }
  if (!finished) throw new Error("Connection closed before the refresh finished");
}
