"use client";

// Recharts wrappers. One file so the chart theme (grid colour, tooltip surface,
// the state palette) is defined once and every chart on the overview page reads
// as one system.

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { clockTime, durationMs } from "@/lib/format";
import type {
  JobStatus,
  LatencyResponse,
  ThroughputResponse,
} from "@/lib/types";

// Recharts takes literal colour strings, not CSS classes, so the palette has to
// exist in JS as well as in the Tailwind config. These are Codity's console
// values in dark mode, matching the tokens in globals.css.
const COLORS = {
  ok: "#3ec98a",
  danger: "#ff5b52",
  warn: "#f5a83c",
  info: "#2f8eff",
  brand: "#7a7fe0",
};

const GRID = "#202020";
const AXIS = "#8e8e8e";

const DEPTH_COLORS: Partial<Record<JobStatus, string>> = {
  queued: COLORS.info,
  scheduled: COLORS.warn,
  claimed: COLORS.brand,
  running: COLORS.brand,
  completed: COLORS.ok,
  failed: COLORS.danger,
  dead: COLORS.danger,
  cancelled: AXIS,
};

// A hover wash has to contrast with the ground it sits on: white at 6%.
const cursorFill = "#ffffff0f";

const tooltipStyle = {
  backgroundColor: "#131313",
  border: `1px solid #303030`,
  borderRadius: 6,
  fontSize: 12,
  color: "#ededed",
};

export function ThroughputChart({ data }: { data: ThroughputResponse }) {
  const rows = data.points.map((p) => ({
    t: clockTime(p.bucket),
    succeeded: p.succeeded,
    failed: p.failed,
    timeout: p.timeout,
    lost: p.lost,
  }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={rows} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
        <defs>
          <linearGradient id="g-ok" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={COLORS.ok} stopOpacity={0.5} />
            <stop offset="100%" stopColor={COLORS.ok} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="t" stroke={AXIS} tick={{ fontSize: 11 }} minTickGap={28} />
        <YAxis stroke={AXIS} tick={{ fontSize: 11 }} allowDecimals={false} />
        <Tooltip contentStyle={tooltipStyle} />
        <Area
          type="monotone"
          dataKey="succeeded"
          stackId="1"
          stroke={COLORS.ok}
          fill="url(#g-ok)"
        />
        <Area
          type="monotone"
          dataKey="failed"
          stackId="1"
          stroke={COLORS.danger}
          fill={COLORS.danger}
          fillOpacity={0.25}
        />
        <Area
          type="monotone"
          dataKey="timeout"
          stackId="1"
          stroke={COLORS.warn}
          fill={COLORS.warn}
          fillOpacity={0.25}
        />
        <Area
          type="monotone"
          dataKey="lost"
          stackId="1"
          stroke={COLORS.danger}
          fill={COLORS.danger}
          fillOpacity={0.15}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function LatencyChart({ data }: { data: LatencyResponse }) {
  const rows = data.points.map((p) => ({
    t: clockTime(p.bucket),
    p50: p.p50_ms,
    p95: p.p95_ms,
  }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={rows} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="t" stroke={AXIS} tick={{ fontSize: 11 }} minTickGap={28} />
        <YAxis
          stroke={AXIS}
          tick={{ fontSize: 11 }}
          tickFormatter={(v) => durationMs(v)}
          width={52}
        />
        <Tooltip
          contentStyle={tooltipStyle}
          formatter={(v: number) => durationMs(v)}
        />
        <Line
          type="monotone"
          dataKey="p50"
          stroke={COLORS.info}
          strokeWidth={2}
          dot={false}
          name="p50"
        />
        <Line
          type="monotone"
          dataKey="p95"
          stroke={COLORS.brand}
          strokeWidth={2}
          dot={false}
          name="p95"
        />
      </LineChart>
    </ResponsiveContainer>
  );
}


export function QueueDepthChart({
  data,
}: {
  data: Record<JobStatus, number>;
}) {
  const rows = (Object.keys(data) as JobStatus[])
    .map((status) => ({ status, count: data[status] }))
    .filter((r) => r.count > 0);
  if (rows.length === 0) {
    return (
      <div className="flex h-[220px] items-center justify-center text-sm text-muted">
        No jobs yet
      </div>
    );
  }
  // Horizontal, with the count printed on each row.
  //
  // Job counts span orders of magnitude in normal operation -- a few thousand
  // completed against a handful dead -- and on a shared linear vertical axis
  // that renders every interesting bar as a one-pixel smear beside one huge
  // one. Turning the bars horizontal gives the labels room and printing the
  // value means a category is still legible when its bar is too short to see,
  // which is exactly the case an operator cares about most.
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart
        data={rows}
        layout="vertical"
        margin={{ top: 4, right: 44, left: 8, bottom: 4 }}
        barCategoryGap={10}
      >
        <CartesianGrid stroke={GRID} horizontal={false} />
        <XAxis type="number" stroke={AXIS} tick={{ fontSize: 11 }} allowDecimals={false} />
        <YAxis
          type="category"
          dataKey="status"
          stroke={AXIS}
          tick={{ fontSize: 11 }}
          width={74}
          tickLine={false}
        />
        {/* Ink at 5%, not white: a white hover wash is invisible on a
            near-white ground. */}
        <Tooltip contentStyle={tooltipStyle} cursor={{ fill: cursorFill }} />
        <Bar dataKey="count" radius={[0, 3, 3, 0]} minPointSize={2}>
          {rows.map((r) => (
            <Cell key={r.status} fill={DEPTH_COLORS[r.status] ?? COLORS.brand} />
          ))}
          <LabelList
            dataKey="count"
            position="right"
            fill={AXIS}
            fontSize={11}
            formatter={(v: number) => v.toLocaleString()}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function Sparkline({ values }: { values: number[] }) {
  const rows = values.map((v, i) => ({ i, v }));
  return (
    <ResponsiveContainer width="100%" height={36}>
      <LineChart data={rows} margin={{ top: 2, right: 2, left: 2, bottom: 2 }}>
        <Line
          type="monotone"
          dataKey="v"
          stroke={COLORS.brand}
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
