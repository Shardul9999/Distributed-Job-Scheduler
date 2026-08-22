"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { Dot } from "./ui";
import {
  IconChevron,
  IconDeadLetters,
  IconJobs,
  IconOverview,
  IconQueues,
  IconSchedules,
  IconSignOut,
  IconWorkers,
} from "./icons";

const NAV = [
  { href: "/", label: "Overview", Icon: IconOverview },
  { href: "/queues", label: "Queues", Icon: IconQueues },
  { href: "/jobs", label: "Job Explorer", Icon: IconJobs },
  { href: "/workers", label: "Workers", Icon: IconWorkers },
  { href: "/schedules", label: "Schedules", Icon: IconSchedules },
  { href: "/dlq", label: "Dead Letters", Icon: IconDeadLetters },
];

export function Sidebar({ connected }: { connected: boolean }) {
  const pathname = usePathname();
  const { me, projects, projectId, setProjectId, logout } = useAuth();

  return (
    // codity.ai pairs near-white content with deep-violet panels; this rail is
    // that panel, using their gradient verbatim. It is also what makes the
    // dashboard read as Codity's at a glance rather than as a generic console.
    <aside className="on-violet-scroll flex h-screen w-60 flex-col bg-brand-gradient text-on-violet">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <span className="flex h-7 w-7 items-center justify-center rounded bg-white/95 text-[13px] font-bold text-brand-ink">
          C
        </span>
        <div className="leading-none">
          <div className="text-[13px] font-semibold tracking-tight">Codity</div>
          <div className="mt-1 text-[11px] text-on-violet-muted">
            Job Scheduler
          </div>
        </div>
      </div>

      <div className="px-3 pb-3">
        <label className="mb-1.5 block text-[10px] font-medium uppercase tracking-[0.08em] text-on-violet-muted">
          Project
        </label>
        {/* Translucent rather than white: a solid input would punch a hole in
            the gradient. Native select with a custom chevron, because the
            browser's own arrow cannot be themed. */}
        <div className="relative">
          <select
            className="w-full appearance-none rounded border border-white/15 bg-white/10 px-3 py-1.5 pr-8
              text-sm text-on-violet outline-none transition-colors
              hover:bg-white/[0.14] focus:border-white/40"
            value={projectId ?? ""}
            onChange={(e) => setProjectId(e.target.value)}
          >
            {projects.length === 0 && (
              <option value="" className="text-fg">
                No projects
              </option>
            )}
            {projects.map((p) => (
              <option key={p.id} value={p.id} className="text-fg">
                {p.name}
              </option>
            ))}
          </select>
          <IconChevron className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-on-violet-muted" />
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 px-3">
        {NAV.map(({ href, label, Icon }) => {
          const active =
            href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={`group relative flex items-center gap-2.5 rounded px-2.5 py-[7px] text-[13px] transition-colors duration-150 ${
                active
                  ? "bg-white/[0.14] font-medium text-white"
                  : "text-on-violet-muted hover:bg-white/[0.07] hover:text-on-violet"
              }`}
            >
              <span
                className={`absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-white transition-opacity ${
                  active ? "opacity-100" : "opacity-0"
                }`}
              />
              <Icon
                className={`h-[15px] w-[15px] shrink-0 transition-colors ${
                  active ? "text-white" : "text-on-violet-muted group-hover:text-on-violet"
                }`}
              />
              {label}
            </Link>
          );
        })}
      </nav>

      <div className="space-y-2.5 border-t border-white/10 px-3 py-3">
        <div className="flex items-center gap-2 text-[11px] text-on-violet-muted">
          <Dot tone={connected ? "ok" : "warn"} pulse={connected} />
          {connected ? "Live" : "Reconnecting…"}
        </div>
        <div
          className="truncate text-[11px] text-on-violet-muted"
          title={me?.user.email}
        >
          {me?.user.full_name ?? me?.user.email ?? "—"}
        </div>
        <button
          className="inline-flex w-full items-center justify-center gap-2 rounded border border-white/15
            bg-white/10 px-3 py-1.5 text-sm font-medium text-on-violet transition-colors
            hover:bg-white/[0.16]"
          onClick={logout}
        >
          <IconSignOut className="h-3.5 w-3.5" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
