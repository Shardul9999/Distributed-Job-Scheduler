"use client";

import { useLive } from "@/components/AppShell";
import {
  LatencyChart,
  QueueDepthChart,
  ThroughputChart,
} from "@/components/charts";
import { Card, Dot, PageHeader, Spinner, StatTile } from "@/components/ui";
import { useLatency, useThroughput } from "@/lib/hooks";
import { durationMs, num, pct } from "@/lib/format";

export default function OverviewPage() {
  // Headline tiles come from the SSE stream so they update every 2s without a
  // poll; the charts poll their own windowed aggregates.
  const { snapshot } = useLive();
  const throughput = useThroughput(3600, 60);
  const latency = useLatency(3600, 60);

  const fleet = snapshot?.fleet;
  const health = snapshot?.health;

  return (
    <div>
      <PageHeader
        title="Overview"
        description="Whole-system health, updated live over SSE."
      />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Active workers"
          value={fleet ? num(fleet.workers_active) : "—"}
          sub={fleet ? `capacity ${num(fleet.fleet_capacity)} slots` : undefined}
          tone={fleet && fleet.workers_active > 0 ? "ok" : "warn"}
        />
        <StatTile
          label="In flight"
          value={fleet ? num(fleet.jobs_in_flight) : "—"}
          sub={fleet ? `${num(fleet.jobs_backlog)} in backlog` : undefined}
          tone="info"
        />
        <StatTile
          label="Success rate (5m)"
          value={health ? pct(health.success_rate) : "—"}
          sub={
            health ? `${num(health.executions_total)} executions` : undefined
          }
          tone={
            health?.success_rate == null
              ? "default"
              : health.success_rate >= 0.95
                ? "ok"
                : health.success_rate >= 0.8
                  ? "warn"
                  : "danger"
          }
        />
        <StatTile
          label="Dead letters"
          value={fleet ? num(fleet.dlq_unreplayed) : "—"}
          sub={fleet ? `${num(fleet.jobs_dead)} dead jobs` : undefined}
          tone={fleet && fleet.dlq_unreplayed > 0 ? "danger" : "ok"}
        />
      </div>

      {/* The single most important signal: is a scheduler leading? Without one,
          cron stops firing and orphans stop being recovered. */}
      <Card className="mt-4 flex items-center justify-between p-4">
        <div className="flex items-center gap-3">
          <Dot tone={fleet?.scheduler_leader_present ? "ok" : "danger"} />
          <div>
            <div className="text-sm font-medium">Scheduler leader</div>
            <div className="text-xs text-muted">
              Advisory-lock elected singleton — cron & crash recovery
            </div>
          </div>
        </div>
        <div className="text-sm">
          {fleet == null
            ? "—"
            : fleet.scheduler_leader_present
              ? "Present"
              : "NONE — cron and reaper are stalled"}
        </div>
      </Card>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card className="p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Throughput (1h)</h2>
            <span className="text-xs text-muted">executions / min, by outcome</span>
          </div>
          {throughput.data ? (
            <ThroughputChart data={throughput.data} />
          ) : (
            <Spinner />
          )}
        </Card>

        <Card className="p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Queue depth</h2>
            <span className="text-xs text-muted">jobs by status</span>
          </div>
          {health ? (
            <QueueDepthChart data={health.jobs_by_status} />
          ) : (
            <Spinner />
          )}
        </Card>

        <Card className="p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Latency percentiles (1h)</h2>
            <div className="flex gap-4 text-xs text-muted">
              <span>p50 {durationMs(latency.data?.p50_ms)}</span>
              <span>p95 {durationMs(latency.data?.p95_ms)}</span>
              <span>p99 {durationMs(latency.data?.p99_ms)}</span>
            </div>
          </div>
          {latency.data ? <LatencyChart data={latency.data} /> : <Spinner />}
        </Card>
      </div>
    </div>
  );
}
