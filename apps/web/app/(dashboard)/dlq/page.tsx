"use client";

import { useState } from "react";
import {
  Badge,
  Card,
  Drawer,
  EmptyState,
  Json,
  KeyValue,
  PageHeader,
  Spinner,
} from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { useDlq, useDlqEntry, useDlqMutations } from "@/lib/hooks";
import { relativeTime, shortId } from "@/lib/format";
import type { DeadLetterEntry } from "@/lib/types";

function DlqDetail({
  entry: listRow,
  projectId,
  onClose,
}: {
  entry: DeadLetterEntry;
  projectId: string;
  onClose: () => void;
}) {
  const { replay, discard } = useDlqMutations(projectId);
  // Re-fetch the entry on open. The list row is already complete enough to
  // render instantly (so the drawer never flashes empty), but only the detail
  // endpoint generates the AI failure summary, so opening the drawer is what
  // triggers it. Fall back to the list row until the fetch lands.
  const { data: fetched, isLoading: summaryLoading } = useDlqEntry(
    projectId,
    listRow.id,
  );
  const entry = fetched ?? listRow;
  const replayed = entry.replayed_at != null;

  return (
    <Drawer
      open
      onClose={onClose}
      title={
        <div className="flex items-center gap-2">
          <span>{entry.job_type}</span>
          {replayed && (
            <Badge className="bg-ok/15 text-ok">replayed</Badge>
          )}
        </div>
      }
    >
      <div className="space-y-6">
        <div className="flex gap-2">
          <button
            className="btn btn-brand"
            disabled={replayed || replay.isPending}
            onClick={() =>
              replay.mutate(entry.id, { onSuccess: onClose })
            }
          >
            Replay
          </button>
          <button
            className="btn btn-danger"
            disabled={discard.isPending}
            onClick={() => discard.mutate(entry.id, { onSuccess: onClose })}
          >
            Discard
          </button>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <KeyValue label="Original job">{shortId(entry.job_id)}</KeyValue>
          <KeyValue label="Attempts">{entry.total_attempts}</KeyValue>
          <KeyValue label="Died">{relativeTime(entry.died_at)}</KeyValue>
          {entry.replayed_job_id && (
            <KeyValue label="Replayed as">
              {shortId(entry.replayed_job_id)}
            </KeyValue>
          )}
        </div>

        {/* Shown while the detail request is in flight, because that request is
            what generates the summary. Once it resolves with nothing, the
            feature is simply off (no API key configured) and the block hides
            rather than showing an empty promise. */}
        {(entry.ai_summary || summaryLoading) && (
          <div>
            <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-wide text-muted">
              AI failure summary
              <Badge className="bg-brand/15 text-brand">beta</Badge>
            </div>
            <div className="rounded-lg border border-brand/30 bg-brand/10 p-3 text-sm">
              {entry.ai_summary ?? (
                <span className="flex items-center gap-2 text-muted">
                  <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-border border-t-brand" />
                  Analysing failure…
                </span>
              )}
            </div>
          </div>
        )}

        <div>
          <div className="mb-1 text-xs uppercase tracking-wide text-muted">
            Failure reason
          </div>
          <div className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
            {entry.failure_reason}
          </div>
        </div>

        {entry.error_stack && (
          <div>
            <div className="mb-1 text-xs uppercase tracking-wide text-muted">
              Stack trace
            </div>
            <pre className="max-h-72 overflow-auto rounded-lg border border-border bg-bg p-3 text-xs">
              {entry.error_stack}
            </pre>
          </div>
        )}

        <div>
          <div className="mb-1 text-xs uppercase tracking-wide text-muted">
            Original payload
          </div>
          <Json value={entry.original_payload} />
        </div>
      </div>
    </Drawer>
  );
}

export default function DlqPage() {
  const { projectId } = useAuth();
  const [unreplayedOnly, setUnreplayedOnly] = useState(true);
  const dlq = useDlq(projectId, unreplayedOnly);
  const [selected, setSelected] = useState<DeadLetterEntry | null>(null);

  return (
    <div>
      <PageHeader
        title="Dead Letters"
        description="Jobs that exhausted every retry. Replay inserts a fresh job and links back — the forensic record is preserved, not mutated."
        actions={
          <label className="flex items-center gap-2 text-sm text-muted">
            <input
              type="checkbox"
              checked={unreplayedOnly}
              onChange={(e) => setUnreplayedOnly(e.target.checked)}
            />
            Unreplayed only
          </label>
        }
      />
      <Card>
        {dlq.isLoading ? (
          <Spinner />
        ) : !dlq.data || dlq.data.items.length === 0 ? (
          <EmptyState>
            {unreplayedOnly
              ? "No unhandled dead letters. "
              : "The dead letter queue is empty."}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Job type</th>
                  <th className="th">Failure reason</th>
                  <th className="th text-right">Attempts</th>
                  <th className="th">Died</th>
                  <th className="th">State</th>
                </tr>
              </thead>
              <tbody>
                {dlq.data.items.map((e) => (
                  <tr
                    key={e.id}
                    className="row cursor-pointer"
                    onClick={() => setSelected(e)}
                  >
                    <td className="td font-medium">{e.job_type}</td>
                    <td className="td max-w-md truncate text-danger">
                      {e.failure_reason}
                    </td>
                    <td className="td text-right tabular-nums">
                      {e.total_attempts}
                    </td>
                    <td className="td text-muted">{relativeTime(e.died_at)}</td>
                    <td className="td">
                      {e.replayed_at ? (
                        <Badge className="bg-ok/15 text-ok">replayed</Badge>
                      ) : (
                        <Badge className="bg-danger/15 text-danger">dead</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {selected && projectId && (
        <DlqDetail
          entry={selected}
          projectId={projectId}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}
