"use client";

import { useState } from "react";
import { useLive } from "@/components/AppShell";
import {
  Card,
  Dot,
  EmptyState,
  PageHeader,
  Spinner,
  StatTile,
  WorkerStatusBadge,
} from "@/components/ui";
import { useWorkers } from "@/lib/hooks";
import { ageSeconds, num, relativeTime, shortId } from "@/lib/format";

// A worker is declared dead by the reaper after 60s of silence; anything past
// ~20s is worth flagging amber before that.
function freshness(ageS: number): "ok" | "warn" | "danger" {
  if (ageS < 20) return "ok";
  if (ageS < 60) return "warn";
  return "danger";
}

export default function WorkersPage() {
  const [includeStopped, setIncludeStopped] = useState(false);
  const workers = useWorkers(includeStopped);
  const { snapshot } = useLive();
  const fleet = snapshot?.fleet;

  return (
    <div>
      <PageHeader
        title="Workers"
        description="Self-registering worker processes. Heartbeat freshness is the liveness signal the reaper acts on."
        actions={
          <label className="flex items-center gap-2 text-sm text-muted">
            <input
              type="checkbox"
              checked={includeStopped}
              onChange={(e) => setIncludeStopped(e.target.checked)}
            />
            Show stopped/dead
          </label>
        }
      />

      <div className="mb-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Active"
          value={fleet ? num(fleet.workers_active) : "—"}
          tone="ok"
        />
        <StatTile
          label="Draining"
          value={fleet ? num(fleet.workers_draining) : "—"}
          tone="warn"
        />
        <StatTile
          label="Dead"
          value={fleet ? num(fleet.workers_dead) : "—"}
          tone={fleet && fleet.workers_dead > 0 ? "danger" : "default"}
        />
        <StatTile
          label="Fleet capacity"
          value={fleet ? num(fleet.fleet_capacity) : "—"}
          sub="concurrent slots"
          tone="info"
        />
      </div>

      <Card>
        {workers.isLoading ? (
          <Spinner />
        ) : !workers.data || workers.data.length === 0 ? (
          <EmptyState>No workers registered.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Worker</th>
                  <th className="th">Status</th>
                  <th className="th">Queues</th>
                  <th className="th text-right">Concurrency</th>
                  <th className="th text-right">Processed</th>
                  <th className="th">Heartbeat</th>
                  <th className="th">Started</th>
                </tr>
              </thead>
              <tbody>
                {workers.data.map((w) => (
                  <tr key={w.id} className="row">
                    <td className="td">
                      <div className="font-medium">{w.hostname}</div>
                      <div className="font-mono text-xs text-muted">
                        {shortId(w.id)} · pid {w.pid}
                      </div>
                    </td>
                    <td className="td">
                      <WorkerStatusBadge status={w.status} />
                    </td>
                    <td className="td text-xs text-muted">
                      {w.queue_names?.join(", ") || "all"}
                    </td>
                    <td className="td text-right tabular-nums">
                      {w.concurrency}
                    </td>
                    <td className="td text-right tabular-nums">
                      {num(w.jobs_processed)}
                    </td>
                    <td className="td">
                      <span className="flex items-center gap-2">
                        <Dot
                          tone={
                            w.status === "active"
                              ? freshness(w.heartbeat_age_s)
                              : "muted"
                          }
                        />
                        {ageSeconds(w.heartbeat_age_s)} ago
                      </span>
                    </td>
                    <td className="td text-muted">
                      {relativeTime(w.started_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
