"use client";

import { Card, EmptyState, PageHeader, Spinner, Badge } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  useQueueMutations,
  useQueueStats,
  useQueues,
} from "@/lib/hooks";
import { durationMs, num } from "@/lib/format";
import type { Queue } from "@/lib/types";

function QueueRow({ queue }: { queue: Queue }) {
  const stats = useQueueStats(queue.id);
  const { can } = useAuth();
  const canWrite = can("member");
  const { pause, resume } = useQueueMutations();
  const paused = stats.data?.is_paused ?? queue.is_paused;
  const busy = pause.isPending || resume.isPending;

  return (
    <tr className="row">
      <td className="td">
        <div className="font-medium">{queue.name}</div>
        <div className="text-xs text-muted">
          priority {queue.priority} · concurrency {queue.max_concurrency}
          {queue.rate_limit_per_sec != null &&
            ` · ${queue.rate_limit_per_sec}/s`}
        </div>
      </td>
      <td className="td text-right tabular-nums">
        {stats.data ? num(stats.data.backlog) : "—"}
      </td>
      <td className="td text-right tabular-nums">
        {stats.data ? num(stats.data.in_flight) : "—"}
      </td>
      <td className="td text-right tabular-nums text-ok">
        {stats.data ? num(stats.data.completed_last_hour) : "—"}
      </td>
      <td className="td text-right tabular-nums text-danger">
        {stats.data ? num(stats.data.failed_last_hour) : "—"}
      </td>
      <td className="td text-right tabular-nums">
        {durationMs(stats.data?.avg_duration_ms)}
      </td>
      <td className="td">
        {paused ? (
          <Badge className="bg-warn/15 text-warn">paused</Badge>
        ) : (
          <Badge className="bg-ok/15 text-ok">active</Badge>
        )}
      </td>
      <td className="td text-right">
        <button
          className="btn"
          disabled={busy || !canWrite}
          title={canWrite ? undefined : "Requires the member role"}
          onClick={() =>
            paused ? resume.mutate(queue.id) : pause.mutate(queue.id)
          }
        >
          {paused ? "Resume" : "Pause"}
        </button>
      </td>
    </tr>
  );
}

export default function QueuesPage() {
  const { projectId } = useAuth();
  const queues = useQueues(projectId);

  return (
    <div>
      <PageHeader
        title="Queues"
        description="Per-queue depth and throughput. Pausing stops claiming; jobs in flight still finish."
      />
      <Card>
        {queues.isLoading ? (
          <Spinner />
        ) : !queues.data || queues.data.length === 0 ? (
          <EmptyState>No queues in this project yet.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Queue</th>
                  <th className="th text-right">Backlog</th>
                  <th className="th text-right">In flight</th>
                  <th className="th text-right">Done 1h</th>
                  <th className="th text-right">Failed 1h</th>
                  <th className="th text-right">Avg dur</th>
                  <th className="th">State</th>
                  <th className="th text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {queues.data.map((q) => (
                  <QueueRow key={q.id} queue={q} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
