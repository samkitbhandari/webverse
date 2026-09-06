"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity, GitBranch, Network, Radar, ScrollText, Split,
} from "lucide-react";
import type { ConnectionState } from "@/lib/websocket";

const LINKS = [
  { href: "/", label: "Overview", icon: Activity },
  { href: "/graph", label: "Graph", icon: Network },
  { href: "/impacts", label: "Impact", icon: Radar },
  { href: "/decisions", label: "Decisions", icon: GitBranch },
  { href: "/scenarios", label: "Scenarios", icon: Split },
  { href: "/entities", label: "Knowledge", icon: ScrollText },
];

export function Nav({ state, nodes }: { state: ConnectionState; nodes?: number }) {
  const pathname = usePathname();
  const tone =
    state === "open" ? "bg-good" : state === "connecting" ? "bg-warn" : "bg-danger";

  return (
    <header className="flex shrink-0 items-center gap-6 border-b border-ink-700/70 bg-ink-900/80 px-4 py-2 backdrop-blur">
      <Link href="/" className="flex items-baseline gap-2">
        <span className="text-sm font-semibold tracking-tight text-ink-100">NEXUS</span>
        <span className="text-sm font-light text-signal">Ω</span>
        <span className="hidden text-[10px] uppercase tracking-[0.18em] text-ink-400 sm:inline">
          knowledge intelligence
        </span>
      </Link>

      <nav className="flex flex-1 items-center gap-1 overflow-x-auto">
        {LINKS.map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={clsx(
                "flex items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs font-medium transition",
                active
                  ? "bg-ink-700/70 text-ink-100"
                  : "text-ink-300 hover:bg-ink-800/70 hover:text-ink-100",
              )}
            >
              <Icon size={13} strokeWidth={2} />
              {label}
            </Link>
          );
        })}
      </nav>

      <div className="flex items-center gap-3 text-[11px] text-ink-400">
        {nodes !== undefined && (
          <span className="metric hidden sm:inline">{nodes} nodes</span>
        )}
        <span className="flex items-center gap-1.5">
          <span className="relative flex h-2 w-2">
            {state === "open" && (
              <span className="absolute inline-flex h-full w-full animate-pulseRing rounded-full bg-good" />
            )}
            <span className={clsx("relative inline-flex h-2 w-2 rounded-full", tone)} />
          </span>
          {state}
        </span>
      </div>
    </header>
  );
}
