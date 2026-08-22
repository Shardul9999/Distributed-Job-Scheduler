"use client";

import { useMemo, useState } from "react";
import {
  Card,
  Drawer,
  EmptyState,
  ExecStatusBadge,
  Json,
  JobStatusBadge,
  KeyValue,
  PageHeader,
  Spinner,
} from "@/components/ui";
import { IconChevron } from "@/components/icons";
import { useAuth } from "@/lib/auth";
import { useJobDetail, useJobMutations, useJobs, useQueues } from "@/lib/hooks";
import { durationMs, relativeTime, shortId } from "@/lib/format";
import type { JobStatus } from "@/lib/types";

const STATUSES: JobStatus[] = [
  "queued",
  "scheduled",
  "claimed",
  "running",
  "completed",
  "failed",
  "dead",
  "cancelled",
];

function JobDetail({
  projectId,
  jobId,
  onClose,
}: {
  projectId: string;
  jobId: string;
  onClose: () => void;
}) {
  const detail = useJobDetail(projectId, jobId);
  const { retry, cancel } = useJobMutations(projectId);

  const job = detail.data?.job;
  const executions = detail.data?.executions ?? [];
  const logs = detail.data?.logs ?? [];

  const canRetry =
    job && ["failed", "dead", "cancelled"].includes(job.status);
  const canCancel =
    job && ["queued", "scheduled"].includes(job.status);

  return (
    <Drawer
      open
      onClose={onClose}
      title={
        job ? (
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm">{shortId(job.id)}</span>
            <JobStatusBadge status={job.status} />
          </div>
        ) : (
          "Job"
        )
      }
    >
      {!job ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          <div className="flex gap-2">
            <button
              className="btn btn-brand"
              disabled={!canRetry || retry.isPending}
              onClick={() => retry.mutate(job.id)}
            >
              Retry
            </button>
            <button
              className="btn"
              disabled={!canCancel || cancel.isPending}
              onClick={() => cancel.mutate(job.id)}
            >
              Cancel
            </button>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <KeyValue label="Type">{job.job_type}</KeyValue>
            <KeyValue label="Attempt">
              {job.attempt} / {job.max_attempts}
            </KeyValue>
            <KeyValue label="Priority">{job.priority}</KeyValue>
            <KeyValue label="Timeout">{job.timeout_s}s</KeyValue>
            <KeyValue label="Run at">{relativeTime(job.run_at)}</KeyValue>
            <KeyValue label="Created">{relativeTime(job.created_at)}</KeyValue>
            {job.batch_id && (
              <KeyValue label="Batch">{shortId(job.batch_id)}</KeyValue>
            )}
            {job.idempotency_key && (
              <KeyValue label="Idempotency">{job.idempotency_key}</KeyValue>
            )}
          </div>

          {job.last_error && (
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted">
                Last error
              </div>
              <div className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
                {job.last_error}
              </div>
            </div>
          )}

          <div>
            <div className="mb-1 text-xs uppercase tracking-wide text-muted">
              Payload
            </div>
            <Json value={job.payload} />
          </div>

          <div>
            <div className="mb-2 text-xs uppercase tracking-wide text-muted">
              Execution history ({executions.length})
            </div>
            {executions.length === 0 ? (
              <div className="text-sm text-muted">No attempts recorded yet.</div>
            ) : (
              <div className="space-y-2">
                {executions.map((ex) => (
                  <div
                    key={ex.id}
                    className="rounded-lg border border-border bg-panel2 p-3"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">
                          Attempt {ex.attempt_number}
                        </span>
                        <ExecStatusBadge status={ex.status} />
                      </div>
                      <span className="text-xs text-muted">
                        {durationMs(ex.duration_ms)} ·{" "}
                        {relativeTime(ex.started_at)}
                      </span>
                    </div>
                    {ex.error_message && (
                      <div className="mt-2 text-xs text-danger">
                        {ex.error_message}
                      </div>
                    )}
                    {ex.worker_id && (
                      <div className="mt-1 font-mono text-xs text-muted">
                        worker {shortId(ex.worker_id)}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <div className="mb-2 text-xs uppercase tracking-wide text-muted">
              Logs ({logs.length})
            </div>
            {logs.length === 0 ? (
              <div className="text-sm text-muted">No log lines.</div>
            ) : (
              <div className="max-h-64 space-y-1 overflow-auto rounded-lg border border-border bg-head p-3 font-mono text-xs">
                {logs.map((l) => (
                  <div key={l.id} className="flex gap-2">
                    <span className="text-muted">
                      {relativeTime(l.logged_at)}
                    </span>
                    <span className="uppercase text-info">{l.level}</span>
                    <span>{l.message}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Drawer>
  );
}

export default function JobsPage() {
  const { projectId } = useAuth();
  const queues = useQueues(projectId);

  const [queueId, setQueueId] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [jobType, setJobType] = useState<string>("");
  const [cursorStack, setCursorStack] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  const cursor = cursorStack[cursorStack.length - 1];
  const filters = useMemo(
    () => ({
      queue_id: queueId || undefined,
      status: status || undefined,
      job_type: jobType || undefined,
      cursor,
    }),
    [queueId, status, jobType, cursor],
  );
  const jobs = useJobs(projectId, filters);

  function resetPaging() {
    setCursorStack([]);
  }

  return (
    <div>
      <PageHeader
        title="Job Explorer"
        description="Filter and page through every job. Keyset pagination — constant cost at any depth."
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <select
          className="input"
          value={queueId}
          onChange={(e) => {
            setQueueId(e.target.value);
            resetPaging();
          }}
        >
          <option value="">All queues</option>
          {queues.data?.map((q) => (
            <option key={q.id} value={q.id}>
              {q.name}
            </option>
          ))}
        </select>
        <select
          className="input"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            resetPaging();
          }}
        >
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <input
          className="input"
          placeholder="job type…"
          value={jobType}
          onChange={(e) => {
            setJobType(e.target.value);
            resetPaging();
          }}
        />
      </div>

      <Card>
        {jobs.isLoading ? (
          <Spinner />
        ) : !jobs.data || jobs.data.items.length === 0 ? (
          <EmptyState>No jobs match these filters.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Job</th>
                  <th className="th">Type</th>
                  <th className="th">Status</th>
                  <th className="th text-right">Attempt</th>
                  <th className="th">Created</th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.items.map((job) => (
                  <tr
                    key={job.id}
                    className="row cursor-pointer"
                    onClick={() => setSelected(job.id)}
                  >
                    <td className="td font-mono">{shortId(job.id)}</td>
                    <td className="td">{job.job_type}</td>
                    <td className="td">
                      <JobStatusBadge status={job.status} />
                    </td>
                    <td className="td text-right tabular-nums">
                      {job.attempt}/{job.max_attempts}
                    </td>
                    <td className="td text-muted">
                      {relativeTime(job.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="mt-4 flex items-center justify-between">
        <button
          className="btn"
          disabled={cursorStack.length === 0}
          onClick={() => setCursorStack((s) => s.slice(0, -1))}
        >
          <IconChevron className="h-3.5 w-3.5 rotate-90" />
          Prev
        </button>
        <span className="text-xs text-muted">
          page {cursorStack.length + 1}
        </span>
        <button
          className="btn"
          disabled={!jobs.data?.has_more || !jobs.data?.next_cursor}
          onClick={() =>
            jobs.data?.next_cursor &&
            setCursorStack((s) => [...s, jobs.data!.next_cursor!])
          }
        >
          Next
          <IconChevron className="h-3.5 w-3.5 -rotate-90" />
        </button>
      </div>

      {selected && projectId && (
        <JobDetail
          projectId={projectId}
          jobId={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}
