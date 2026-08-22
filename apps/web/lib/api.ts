// The single fetch wrapper every hook goes through. It attaches the bearer
// token, unwraps the API's error envelope into a real Error, and exposes the
// base URL so the SSE hook can build its own EventSource URL with the same
// origin.

import type {
  DeadLetterEntry,
  FleetStats,
  HealthResponse,
  Job,
  JobExecution,
  JobLog,
  LatencyResponse,
  Me,
  Organization,
  Page,
  Project,
  Queue,
  QueueStats,
  Schedule,
  ThroughputResponse,
  TokenPair,
  Worker,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";

const TOKEN_KEY = "codity.access_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  code: string;
  requestId?: string;
  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  // Extra headers, e.g. an Idempotency-Key.
  headers?: Record<string, string>;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...opts.headers,
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;

  if (!res.ok) {
    const env = data?.error;
    // A 401 mid-session means the token expired or was revoked. Clearing it
    // lets the app-level guard bounce the user to /login on the next render.
    if (res.status === 401) setToken(null);
    throw new ApiError(
      res.status,
      env?.code ?? "ERROR",
      env?.message ?? res.statusText,
      env?.request_id,
    );
  }
  return data as T;
}

function qs(params: Record<string, string | number | boolean | undefined | null>) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  // --- auth ---
  login: (email: string, password: string) =>
    request<TokenPair>("/auth/login", { method: "POST", body: { email, password } }),
  register: (body: {
    email: string;
    password: string;
    full_name: string;
    organization_name: string;
  }) => request<TokenPair>("/auth/register", { method: "POST", body }),
  me: () => request<Me>("/auth/me"),

  // --- tenancy ---
  orgs: () => request<Organization[]>("/orgs"),
  projects: (orgId: string) => request<Project[]>(`/orgs/${orgId}/projects`),

  // --- queues ---
  queues: (projectId: string) =>
    request<Queue[]>(`/projects/${projectId}/queues`),
  queueStats: (queueId: string) =>
    request<QueueStats>(`/queues/${queueId}/stats`),
  pauseQueue: (queueId: string) =>
    request<Queue>(`/queues/${queueId}/pause`, { method: "POST" }),
  resumeQueue: (queueId: string) =>
    request<Queue>(`/queues/${queueId}/resume`, { method: "POST" }),

  // --- jobs ---
  jobs: (
    projectId: string,
    filters: {
      queue_id?: string;
      status?: string;
      job_type?: string;
      cursor?: string;
      limit?: number;
    },
  ) => request<Page<Job>>(`/projects/${projectId}/jobs${qs(filters)}`),
  job: (projectId: string, jobId: string) =>
    request<Job>(`/projects/${projectId}/jobs/${jobId}`),
  jobExecutions: (projectId: string, jobId: string) =>
    request<JobExecution[]>(`/projects/${projectId}/jobs/${jobId}/executions`),
  jobLogs: (projectId: string, jobId: string) =>
    request<JobLog[]>(`/projects/${projectId}/jobs/${jobId}/logs`),
  retryJob: (projectId: string, jobId: string) =>
    request<Job>(`/projects/${projectId}/jobs/${jobId}/retry`, { method: "POST" }),
  cancelJob: (projectId: string, jobId: string) =>
    request<Job>(`/projects/${projectId}/jobs/${jobId}/cancel`, { method: "POST" }),
  enqueue: (
    queueId: string,
    body: { job_type: string; payload?: Record<string, unknown>; delay_seconds?: number },
  ) => request<Job>(`/queues/${queueId}/jobs`, { method: "POST", body }),

  // --- schedules ---
  schedules: (projectId: string) =>
    request<Schedule[]>(`/projects/${projectId}/schedules`),
  patchSchedule: (projectId: string, id: string, body: { is_active: boolean }) =>
    request<Schedule>(`/projects/${projectId}/schedules/${id}`, {
      method: "PATCH",
      body,
    }),
  triggerSchedule: (projectId: string, id: string) =>
    request<unknown>(`/projects/${projectId}/schedules/${id}/trigger`, {
      method: "POST",
    }),

  // --- dlq ---
  dlq: (projectId: string, unreplayedOnly?: boolean) =>
    request<Page<DeadLetterEntry>>(
      `/projects/${projectId}/dlq${qs({ unreplayed_only: unreplayedOnly })}`,
    ),
  replayDlq: (projectId: string, id: string) =>
    request<unknown>(`/projects/${projectId}/dlq/${id}/replay`, { method: "POST" }),
  discardDlq: (projectId: string, id: string) =>
    request<void>(`/projects/${projectId}/dlq/${id}`, { method: "DELETE" }),

  // --- fleet & metrics (global) ---
  workers: (includeStopped = false) =>
    request<Worker[]>(`/workers${qs({ include_stopped: includeStopped })}`),
  fleetStats: () => request<FleetStats>("/fleet/stats"),
  throughput: (windowSeconds: number, bucketSeconds: number) =>
    request<ThroughputResponse>(
      `/metrics/throughput${qs({ window_seconds: windowSeconds, bucket_seconds: bucketSeconds })}`,
    ),
  latency: (windowSeconds: number, bucketSeconds: number) =>
    request<LatencyResponse>(
      `/metrics/latency${qs({ window_seconds: windowSeconds, bucket_seconds: bucketSeconds })}`,
    ),
  health: (windowSeconds: number) =>
    request<HealthResponse>(`/metrics/health${qs({ window_seconds: windowSeconds })}`),
};
