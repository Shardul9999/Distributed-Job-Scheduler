"use client";

// TanStack Query hooks. Tables poll on an interval as a reliable fallback; the
// overview's headline numbers come from the SSE stream instead, which is why
// there is no fleet-stats polling hook here -- useLiveSnapshot covers it.

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "./api";

const REFRESH = 5000; // 5s: brisk enough to feel live, cheap for the API.

export function useQueues(projectId: string | null) {
  return useQuery({
    queryKey: ["queues", projectId],
    queryFn: () => api.queues(projectId!),
    enabled: !!projectId,
    refetchInterval: REFRESH,
  });
}

export function useQueueStats(queueId: string | null) {
  return useQuery({
    queryKey: ["queue-stats", queueId],
    queryFn: () => api.queueStats(queueId!),
    enabled: !!queueId,
    refetchInterval: REFRESH,
  });
}

export function useJobs(
  projectId: string | null,
  filters: { queue_id?: string; status?: string; job_type?: string; cursor?: string },
) {
  return useQuery({
    queryKey: ["jobs", projectId, filters],
    queryFn: () => api.jobs(projectId!, { ...filters, limit: 50 }),
    enabled: !!projectId,
    refetchInterval: REFRESH,
    placeholderData: (prev) => prev,
  });
}

export function useJobDetail(projectId: string | null, jobId: string | null) {
  return useQuery({
    queryKey: ["job", projectId, jobId],
    queryFn: async () => {
      const [job, executions, logs] = await Promise.all([
        api.job(projectId!, jobId!),
        api.jobExecutions(projectId!, jobId!),
        api.jobLogs(projectId!, jobId!),
      ]);
      return { job, executions, logs };
    },
    enabled: !!projectId && !!jobId,
    refetchInterval: REFRESH,
  });
}

export function useSchedules(projectId: string | null) {
  return useQuery({
    queryKey: ["schedules", projectId],
    queryFn: () => api.schedules(projectId!),
    enabled: !!projectId,
    refetchInterval: REFRESH,
  });
}

export function useDlq(projectId: string | null, unreplayedOnly: boolean) {
  return useQuery({
    queryKey: ["dlq", projectId, unreplayedOnly],
    queryFn: () => api.dlq(projectId!, unreplayedOnly),
    enabled: !!projectId,
    refetchInterval: REFRESH,
  });
}

export function useWorkers(includeStopped: boolean) {
  return useQuery({
    queryKey: ["workers", includeStopped],
    queryFn: () => api.workers(includeStopped),
    refetchInterval: 3000,
  });
}

export function useThroughput(windowSeconds: number, bucketSeconds: number) {
  return useQuery({
    queryKey: ["throughput", windowSeconds, bucketSeconds],
    queryFn: () => api.throughput(windowSeconds, bucketSeconds),
    refetchInterval: REFRESH,
  });
}

export function useLatency(windowSeconds: number, bucketSeconds: number) {
  return useQuery({
    queryKey: ["latency", windowSeconds, bucketSeconds],
    queryFn: () => api.latency(windowSeconds, bucketSeconds),
    refetchInterval: REFRESH,
  });
}

// --- mutations ---

export function useQueueMutations() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["queues"] });
  return {
    pause: useMutation({ mutationFn: api.pauseQueue, onSuccess: invalidate }),
    resume: useMutation({ mutationFn: api.resumeQueue, onSuccess: invalidate }),
  };
}

export function useJobMutations(projectId: string | null) {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["job"] });
  };
  return {
    retry: useMutation({
      mutationFn: (jobId: string) => api.retryJob(projectId!, jobId),
      onSuccess: invalidate,
    }),
    cancel: useMutation({
      mutationFn: (jobId: string) => api.cancelJob(projectId!, jobId),
      onSuccess: invalidate,
    }),
  };
}

export function useScheduleMutations(projectId: string | null) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["schedules"] });
  return {
    toggle: useMutation({
      mutationFn: (v: { id: string; is_active: boolean }) =>
        api.patchSchedule(projectId!, v.id, { is_active: v.is_active }),
      onSuccess: invalidate,
    }),
    trigger: useMutation({
      mutationFn: (id: string) => api.triggerSchedule(projectId!, id),
      onSuccess: invalidate,
    }),
  };
}

export function useDlqMutations(projectId: string | null) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["dlq"] });
  return {
    replay: useMutation({
      mutationFn: (id: string) => api.replayDlq(projectId!, id),
      onSuccess: invalidate,
    }),
    discard: useMutation({
      mutationFn: (id: string) => api.discardDlq(projectId!, id),
      onSuccess: invalidate,
    }),
  };
}
