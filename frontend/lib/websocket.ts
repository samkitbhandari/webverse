"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { NexusEvent } from "./types";

export type ConnectionState = "connecting" | "open" | "closed";

/**
 * Subscribe to the NEXUS event stream.
 *
 * Reconnects with backoff, because a demo where the graph silently stops
 * updating after a backend restart is worse than one that visibly reconnects.
 */
export function useNexusStream(limit = 200) {
  const [events, setEvents] = useState<NexusEvent[]>([]);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [lastChange, setLastChange] = useState(0);
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef = useRef(false);

  const connect = useCallback(() => {
    if (typeof window === "undefined" || closedRef.current) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws`);
    socketRef.current = ws;
    setState("connecting");

    ws.onopen = () => {
      attemptRef.current = 0;
      setState("open");
    };

    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as NexusEvent;
        setEvents((prev) => [event, ...prev].slice(0, limit));
        // Anything that alters the graph should make consumers refetch.
        if (
          event.event_type === "GRAPH_CHANGED" ||
          event.event_type === "CONNECTED" ||
          event.event_type.startsWith("CLAIM_") ||
          event.event_type.startsWith("ENTITY_") ||
          event.event_type === "CONTRADICTION_DETECTED" ||
          event.event_type === "CONSTRAINT_VIOLATED"
        ) {
          setLastChange(Date.now());
        }
      } catch {
        /* ignore malformed frames */
      }
    };

    ws.onerror = () => ws.close();

    ws.onclose = () => {
      setState("closed");
      if (closedRef.current) return;
      const delay = Math.min(1000 * 2 ** attemptRef.current, 15000);
      attemptRef.current += 1;
      timerRef.current = setTimeout(connect, delay);
    };
  }, [limit]);

  useEffect(() => {
    closedRef.current = false;
    connect();
    return () => {
      closedRef.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      socketRef.current?.close();
    };
  }, [connect]);

  const clear = useCallback(() => setEvents([]), []);
  return { events, state, lastChange, clear };
}

/** Poll-free data hook that refetches whenever the graph changes. */
export function useLiveData<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loaderRef
      .current()
      .then((value) => {
        if (!cancelled) {
          setData(value);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}
