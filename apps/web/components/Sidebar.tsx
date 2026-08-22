"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { Dot } from "./ui";

const NAV = [
  { href: "/", label: "Overview", icon: "◆" },
  { href: "/queues", label: "Queues", icon: "≡" },
  { href: "/jobs", label: "Job Explorer", icon: "☰" },
  { href: "/workers", label: "Workers", icon: "⚙" },
  { href: "/schedules", label: "Schedules", icon: "◷" },
  { href: "/dlq", label: "Dead Letters", icon: "⚠" },
];

export function Sidebar({ connected }: { connected: boolean }) {
  const pathname = usePathname();
  const { me, projects, projectId, setProjectId, logout } = useAuth();

  return (
    <aside className="flex h-screen w-60 flex-col border-r border-border bg-panel">
      <div className="flex items-center gap-2 px-5 py-4">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand font-bold text-white">
          C
        </span>
        <div>
          <div className="text-sm font-semibold leading-tight">Codity</div>
          <div className="text-xs text-muted">Job Scheduler</div>
        </div>
      </div>

      <div className="px-4 pb-3">
        <label className="mb-1 block text-xs uppercase tracking-wide text-muted">
          Project
        </label>
        <select
          className="input w-full"
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
      </div>

      <nav className="flex-1 px-2">
        {NAV.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                active
                  ? "bg-panel2 font-medium text-fg"
                  : "text-muted hover:bg-panel2 hover:text-fg"
              }`}
            >
              <span className="w-4 text-center text-muted">{item.icon}</span>
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-border px-4 py-3">
        <div className="mb-2 flex items-center gap-2 text-xs text-muted">
          <Dot tone={connected ? "ok" : "warn"} />
          {connected ? "Live" : "Reconnecting…"}
        </div>
        <div className="mb-2 truncate text-xs text-muted" title={me?.email}>
          {me?.email ?? "—"}
        </div>
        <button className="btn w-full" onClick={logout}>
          Sign out
        </button>
      </div>
    </aside>
  );
}
