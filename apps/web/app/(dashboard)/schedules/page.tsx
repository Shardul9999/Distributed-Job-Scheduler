"use client";

import { Badge, Card, EmptyState, PageHeader, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { useScheduleMutations, useSchedules } from "@/lib/hooks";
import { relativeTime } from "@/lib/format";

export default function SchedulesPage() {
  const { projectId, can } = useAuth();
  const canWrite = can("member");
  const schedules = useSchedules(projectId);
  const { toggle, trigger } = useScheduleMutations(projectId);

  return (
    <div>
      <PageHeader
        title="Schedules"
        description="Cron templates materialised into jobs by the leader scheduler, in each schedule's own timezone."
      />
      <Card>
        {schedules.isLoading ? (
          <Spinner />
        ) : !schedules.data || schedules.data.length === 0 ? (
          <EmptyState>No schedules in this project.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Name</th>
                  <th className="th">Cron</th>
                  <th className="th">Timezone</th>
                  <th className="th">Job type</th>
                  <th className="th">Next run</th>
                  <th className="th">Last run</th>
                  <th className="th">State</th>
                  <th className="th text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {schedules.data.map((s) => (
                  <tr key={s.id} className="row">
                    <td className="td font-medium">{s.name}</td>
                    <td className="td font-mono text-xs">{s.cron_expression}</td>
                    <td className="td text-xs text-muted">{s.timezone}</td>
                    <td className="td">{s.job_type}</td>
                    <td className="td">{relativeTime(s.next_run_at)}</td>
                    <td className="td text-muted">
                      {relativeTime(s.last_run_at)}
                    </td>
                    <td className="td">
                      {s.is_active ? (
                        <Badge className="bg-ok/15 text-ok">active</Badge>
                      ) : (
                        <Badge className="bg-muted/15 text-muted">paused</Badge>
                      )}
                    </td>
                    <td className="td">
                      <div className="flex justify-end gap-2">
                        <button
                          className="btn"
                          disabled={!canWrite || trigger.isPending}
                          title={canWrite ? undefined : "Requires the member role"}
                          onClick={() => trigger.mutate(s.id)}
                        >
                          Run now
                        </button>
                        <button
                          className="btn"
                          disabled={!canWrite || toggle.isPending}
                          title={canWrite ? undefined : "Requires the member role"}
                          onClick={() =>
                            toggle.mutate({ id: s.id, is_active: !s.is_active })
                          }
                        >
                          {s.is_active ? "Pause" : "Resume"}
                        </button>
                      </div>
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
