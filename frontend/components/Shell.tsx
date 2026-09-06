"use client";

import { createContext, useContext, useMemo, type ReactNode } from "react";
import { Nav } from "./Nav";
import { useNexusStream, type ConnectionState } from "@/lib/websocket";
import type { NexusEvent } from "@/lib/types";

interface StreamContext {
  events: NexusEvent[];
  state: ConnectionState;
  /** timestamp of the last graph-altering event; use as a refetch dependency */
  lastChange: number;
  nodes: number | undefined;
}

const Ctx = createContext<StreamContext>({
  events: [], state: "connecting", lastChange: 0, nodes: undefined,
});

/** Every page reads the same socket: one connection for the whole app. */
export function useStream() {
  return useContext(Ctx);
}

export function Shell({ children }: { children: ReactNode }) {
  const { events, state, lastChange } = useNexusStream();

  const nodes = useMemo(() => {
    for (const e of events) {
      const n = e.payload?.stats?.nodes;
      if (typeof n === "number") return n;
    }
    return undefined;
  }, [events]);

  const value = useMemo(
    () => ({ events, state, lastChange, nodes }),
    [events, state, lastChange, nodes],
  );

  return (
    <Ctx.Provider value={value}>
      <div className="flex h-screen flex-col overflow-hidden">
        <Nav state={state} nodes={nodes} />
        <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
      </div>
    </Ctx.Provider>
  );
}
