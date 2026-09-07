"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { GraphLink, GraphNode, GraphPayload } from "@/lib/types";
import { EDGE_COLOR, RETIRED, STATUS_COLOR, TYPE_COLOR } from "@/lib/theme";

// force-graph reaches for `window` at module scope, so it can only load client-side.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

export interface GraphHighlight {
  /** node id -> 0..1 severity; drives the halo */
  impacted?: Record<string, number>;
  contradicted?: Set<string>;
  recent?: Set<string>;
  path?: string[];
}

interface Props {
  data: GraphPayload | null;
  selected?: string | null;
  highlight?: GraphHighlight;
  onSelect?: (id: string | null) => void;
  className?: string;
}

type SimNode = GraphNode & { x?: number; y?: number; vx?: number; vy?: number; fx?: number; fy?: number };

export function KnowledgeGraph({ data, selected, highlight, onSelect, className }: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<any>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const positions = useRef<Map<string, { x: number; y: number }>>(new Map());

  /**
   * The canvas needs explicit pixel dimensions, so the container is measured.
   *
   * Measuring happens directly rather than *only* inside a ResizeObserver
   * callback. Gating the first render on that callback means that anywhere it
   * does not fire -- some embedded webviews, a container that is laid out
   * before it is observed -- the graph stays permanently blank with no error
   * to explain it. So the size is read synchronously on mount, and the
   * observer is an enhancement for later resizes rather than a prerequisite
   * for drawing anything at all.
   */
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;

    const measure = () => {
      const { width, height } = el.getBoundingClientRect();
      const w = Math.floor(width);
      const h = Math.floor(height);
      setSize((prev) => (prev.w === w && prev.h === h ? prev : { w, h }));
    };

    measure();

    let ro: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(measure);
      ro.observe(el);
    }
    window.addEventListener("resize", measure);
    // Fonts, scrollbars and the panel's own flex sizing can settle a frame or
    // two after mount; one deferred re-measure catches that cheaply.
    const settle = setTimeout(measure, 250);

    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", measure);
      clearTimeout(settle);
    };
  }, []);

  /**
   * The force simulation mutates whatever objects it is handed, so the data is
   * cloned on every update. Previously-known coordinates are carried across,
   * otherwise the whole graph re-explodes from the centre each time a claim
   * lands -- which in a live demo looks like a bug.
   */
  const graphData = useMemo(() => {
    if (!data) return { nodes: [] as SimNode[], links: [] as GraphLink[] };
    const nodes: SimNode[] = data.nodes.map((n) => {
      const prev = positions.current.get(n.id);
      return prev ? { ...n, x: prev.x, y: prev.y } : { ...n };
    });
    const present = new Set(nodes.map((n) => n.id));
    const links = data.links
      .filter((l) => {
        const s = typeof l.source === "string" ? l.source : l.source.id;
        const t = typeof l.target === "string" ? l.target : l.target.id;
        return present.has(s) && present.has(t);
      })
      .map((l) => ({ ...l }));
    return { nodes, links };
  }, [data]);

  const rememberPositions = useCallback(() => {
    for (const n of graphData.nodes) {
      if (typeof n.x === "number" && typeof n.y === "number") {
        positions.current.set(n.id, { x: n.x, y: n.y });
      }
    }
  }, [graphData]);

  const pathEdges = useMemo(() => {
    const set = new Set<string>();
    const path = highlight?.path ?? [];
    for (let i = 0; i < path.length - 1; i += 1) set.add(`${path[i]}->${path[i + 1]}`);
    return set;
  }, [highlight?.path]);

  const pathNodes = useMemo(() => new Set(highlight?.path ?? []), [highlight?.path]);
  const dimmed = pathNodes.size > 0;

  const drawNode = useCallback(
    (node: SimNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const retired = RETIRED.includes(node.status);
      const severity = highlight?.impacted?.[node.id] ?? 0;
      const isSelected = selected === node.id;
      const onPath = pathNodes.has(node.id);
      const isContradicted = highlight?.contradicted?.has(node.id) ?? false;
      const isRecent = highlight?.recent?.has(node.id) ?? false;

      const base = 3.2 + node.criticality * 4.2 + severity * 3.5;
      const fill = TYPE_COLOR[node.type] ?? "#8792a5";
      const ring = STATUS_COLOR[node.status] ?? "#8792a5";
      const faded = dimmed && !onPath;
      const alpha = faded ? 0.12 : retired ? 0.4 : 1;

      ctx.save();
      ctx.globalAlpha = alpha;

      // severity halo
      if (severity > 0.02 && !faded) {
        const halo = ctx.createRadialGradient(x, y, base, x, y, base + 10 + severity * 16);
        halo.addColorStop(0, `${severity > 0.25 ? "#ef476f" : "#f4a261"}55`);
        halo.addColorStop(1, "#00000000");
        ctx.fillStyle = halo;
        ctx.beginPath();
        ctx.arc(x, y, base + 10 + severity * 16, 0, 2 * Math.PI);
        ctx.fill();
      }

      if (isRecent && !faded) {
        ctx.strokeStyle = "#4cc9f0";
        ctx.lineWidth = 1.2 / scale;
        ctx.setLineDash([2 / scale, 2 / scale]);
        ctx.beginPath();
        ctx.arc(x, y, base + 4.5, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      ctx.beginPath();
      ctx.arc(x, y, base, 0, 2 * Math.PI);
      ctx.fillStyle = retired ? "#1f2632" : fill;
      ctx.fill();

      ctx.lineWidth = (isSelected ? 2.4 : isContradicted ? 2 : 1.2) / scale;
      ctx.strokeStyle = isSelected ? "#ffffff" : isContradicted ? "#ef476f" : ring;
      ctx.stroke();

      if (retired) {
        ctx.beginPath();
        ctx.moveTo(x - base * 0.7, y + base * 0.7);
        ctx.lineTo(x + base * 0.7, y - base * 0.7);
        ctx.strokeStyle = "#5b6779";
        ctx.lineWidth = 1 / scale;
        ctx.stroke();
      }

      // labels only when zoomed in enough to read them
      if ((scale > 1.15 || isSelected || onPath) && !faded) {
        const text = node.label.length > 42 ? `${node.label.slice(0, 41)}…` : node.label;
        const fontSize = Math.max(2.6, 10 / scale);
        ctx.font = `${fontSize}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        const w = ctx.measureText(text).width;
        ctx.fillStyle = "rgba(7,9,13,0.78)";
        ctx.fillRect(x - w / 2 - 2 / scale, y + base + 2 / scale, w + 4 / scale, fontSize + 2 / scale);
        ctx.fillStyle = isSelected ? "#ffffff" : "#b6bfcd";
        ctx.fillText(text, x, y + base + 3 / scale);
      }
      ctx.restore();
    },
    [dimmed, highlight, pathNodes, selected],
  );

  const linkColor = useCallback(
    (link: GraphLink) => {
      const s = typeof link.source === "string" ? link.source : link.source.id;
      const t = typeof link.target === "string" ? link.target : link.target.id;
      if (pathEdges.has(`${s}->${t}`) || pathEdges.has(`${t}->${s}`)) return "#4cc9f0";
      if (dimmed) return "#141a2400";
      return `${EDGE_COLOR[link.type] ?? "#2b3442"}${link.type === "CONTRADICTS" ? "" : "aa"}`;
    },
    [dimmed, pathEdges],
  );

  const linkWidth = useCallback(
    (link: GraphLink) => {
      const s = typeof link.source === "string" ? link.source : link.source.id;
      const t = typeof link.target === "string" ? link.target : link.target.id;
      if (pathEdges.has(`${s}->${t}`) || pathEdges.has(`${t}->${s}`)) return 2.6;
      return 0.4 + link.weight * 1.1;
    },
    [pathEdges],
  );

  useEffect(() => {
    if (!fgRef.current || graphData.nodes.length === 0) return;
    const t = setTimeout(() => fgRef.current?.zoomToFit(500, 60), 350);
    return () => clearTimeout(t);
    // only refit when the node count changes materially, not on every tick
  }, [graphData.nodes.length]);

  return (
    <div ref={wrapRef} className={className ?? "h-full w-full"}>
      {size.w > 0 && (
        <ForceGraph2D
          ref={fgRef}
          width={size.w}
          height={size.h}
          graphData={graphData as any}
          backgroundColor="#07090d"
          nodeCanvasObject={drawNode as any}
          nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(node.x ?? 0, node.y ?? 0, 8 + (node.criticality ?? 0) * 4, 0, 2 * Math.PI);
            ctx.fill();
          }}
          linkColor={linkColor as any}
          linkWidth={linkWidth as any}
          linkDirectionalArrowLength={3}
          linkDirectionalArrowRelPos={0.98}
          linkDirectionalParticles={(l: any) =>
            pathEdges.has(`${l.source?.id ?? l.source}->${l.target?.id ?? l.target}`) ? 3 : 0
          }
          linkDirectionalParticleSpeed={0.006}
          linkDirectionalParticleWidth={2}
          onNodeClick={(node: any) => onSelect?.(node.id)}
          onBackgroundClick={() => onSelect?.(null)}
          onEngineStop={rememberPositions}
          cooldownTicks={120}
          d3VelocityDecay={0.28}
          nodeRelSize={4}
        />
      )}
    </div>
  );
}
