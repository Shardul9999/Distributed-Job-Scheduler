"use client";

// useLiveSnapshot subscribes to GET /events (SSE) and returns the most recent
// snapshot plus a connection flag. EventSource cannot set an Authorization
// header, so the access token rides in the query string -- the endpoint
// validates it exactly as the header form.

import { useEffect, useRef, useState } from "react";
import { API_BASE, getToken } from "./api";
import type { LiveSnapshot } from "./types";

export function useLiveSnapshot(intervalSeconds = 2) {
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const token = getToken();
    if (!token) return;

    const url = `${API_BASE}/events?token=${encodeURIComponent(token)}&interval=${intervalSeconds}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener("open", () => setConnected(true));
    es.addEventListener("snapshot", (ev) => {
      try {
        setSnapshot(JSON.parse((ev as MessageEvent).data));
        setConnected(true);
      } catch {
        /* ignore a malformed frame; the next tick recovers */
      }
    });
    es.addEventListener("error", () => {
      // EventSource reconnects on its own; just reflect the gap in the UI.
      setConnected(false);
    });

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [intervalSeconds]);

  return { snapshot, connected };
}
