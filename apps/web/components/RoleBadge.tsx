import type { OrgRole } from "@/lib/types";

/** Ranked roles read better as a ramp than as four unrelated colours: viewer is
 *  quiet, owner carries the brand. Colour here is ordinal, not categorical. */
const TONE: Record<OrgRole, string> = {
  viewer: "bg-panel2 text-muted",
  member: "bg-info/15 text-info",
  admin: "bg-warn/15 text-warn",
  owner: "bg-brand/15 text-brand",
};

export function RoleBadge({
  role,
  prefix,
}: {
  role: OrgRole;
  /** e.g. "You are", rendered quietly ahead of the role. */
  prefix?: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {prefix && <span className="text-[11px] text-muted">{prefix}</span>}
      <span
        className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ${TONE[role]}`}
      >
        {role}
      </span>
    </span>
  );
}
