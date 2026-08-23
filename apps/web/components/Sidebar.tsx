"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { Dot } from "./ui";
import { RoleBadge } from "./RoleBadge";
import {
  IconChevron,
  IconDeadLetters,
  IconJobs,
  IconOverview,
  IconQueues,
  IconSchedules,
  IconSignOut,
  IconTeam,
  IconWorkers,
} from "./icons";

const NAV = [
  { href: "/", label: "Overview", Icon: IconOverview },
  { href: "/queues", label: "Queues", Icon: IconQueues },
  { href: "/jobs", label: "Job Explorer", Icon: IconJobs },
  { href: "/workers", label: "Workers", Icon: IconWorkers },
  { href: "/schedules", label: "Schedules", Icon: IconSchedules },
  { href: "/dlq", label: "Dead Letters", Icon: IconDeadLetters },
  { href: "/team", label: "Team", Icon: IconTeam },
];

export function Sidebar({ connected }: { connected: boolean }) {
  const pathname = usePathname();
  const { me, projects, projectId, setProjectId, logout, role } = useAuth();

  return (
    // The rail sits on the same near-black as the content it navigates; a
    // single hairline separates them. Nothing here competes with the data.
    <aside className="flex h-screen w-60 flex-col border-r border-border bg-panel text-fg">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <span className="flex h-7 w-7 items-center justify-center rounded bg-brand text-[13px] font-bold text-white">
          C
        </span>
        <div className="leading-none">
          <div className="text-[13px] font-semibold tracking-tight">Codity</div>
          <div className="mt-1 text-[11px] text-muted">Job Scheduler</div>
        </div>
      </div>

      <div className="px-3 pb-3">
        <label className="mb-1.5 block text-[10px] font-medium uppercase tracking-[0.08em] text-muted">
          Project
        </label>
        {/* Native select with a custom chevron, because the browser's own arrow
            cannot be themed. */}
        <div className="relative">
          <select
            className="w-full appearance-none rounded border border-border bg-panel2 px-3 py-1.5 pr-8
              text-sm text-fg outline-none transition-colors
              hover:border-strong hover:bg-raised focus:border-brand"
            value={projectId ?? ""}
            onChange={(e) => setProjectId(e.target.value)}
          >
            {projects.length === 0 && <option value="">No projects</option>}
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <IconChevron className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
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
                  ? "bg-panel2 font-medium text-fg"
                  : "text-muted hover:bg-head hover:text-fg"
              }`}
            >
              <span
                className={`absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-brand transition-opacity ${
                  active ? "opacity-100" : "opacity-0"
                }`}
              />
              <Icon
                className={`h-[15px] w-[15px] shrink-0 transition-colors ${
                  active ? "text-brand" : "text-faint group-hover:text-fg"
                }`}
              />
              {label}
            </Link>
          );
        })}
      </nav>

      <div className="space-y-2.5 border-t border-border px-3 py-3">
        <div className="flex items-center gap-2 text-[11px] text-muted">
          <Dot tone={connected ? "ok" : "warn"} pulse={connected} />
          {connected ? "Live" : "Reconnecting…"}
        </div>
        <div className="flex items-center justify-between gap-2">
          <div className="truncate text-[11px] text-muted" title={me?.user.email}>
            {me?.user.full_name ?? me?.user.email ?? "—"}
          </div>
          {role && <RoleBadge role={role} />}
        </div>
        <button className="btn w-full" onClick={logout}>
          <IconSignOut className="h-3.5 w-3.5" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
