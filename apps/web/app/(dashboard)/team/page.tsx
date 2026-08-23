"use client";

// The operator-facing view of RBAC. Without this page the whole role system is
// invisible in the product: the API has enforced ranked roles since Day 0, but
// the only way to see or change one was curl.

import { useState } from "react";
import {
  Badge,
  Card,
  EmptyState,
  PageHeader,
  Spinner,
} from "@/components/ui";
import { RoleBadge } from "@/components/RoleBadge";
import { useAuth } from "@/lib/auth";
import { useMemberMutations, useMembers } from "@/lib/hooks";
import { ApiError } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import type { Member, OrgRole } from "@/lib/types";

const ROLES: { value: OrgRole; label: string; blurb: string }[] = [
  { value: "viewer", label: "Viewer", blurb: "Read every page, change nothing" },
  { value: "member", label: "Member", blurb: "Operate: enqueue, retry, pause, replay" },
  { value: "admin", label: "Admin", blurb: "Delete projects and queues, rotate keys, manage people" },
  { value: "owner", label: "Owner", blurb: "Everything, plus delete the organization" },
];

function InviteForm({ orgId }: { orgId: string }) {
  const { add } = useMemberMutations(orgId);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<OrgRole>("member");
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await add.mutateAsync({ email, role });
      setEmail("");
    } catch (err) {
      // The API is explicit that the person must already have an account --
      // surfacing its own message beats inventing a vaguer one.
      setError(
        err instanceof ApiError ? err.message : "Could not add that member.",
      );
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2">
      <div className="min-w-[220px] flex-1">
        <label className="mb-1 block text-[10px] font-medium uppercase tracking-[0.08em] text-muted">
          Email
        </label>
        <input
          className="input w-full"
          type="email"
          required
          placeholder="teammate@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <div>
        <label className="mb-1 block text-[10px] font-medium uppercase tracking-[0.08em] text-muted">
          Role
        </label>
        <select
          className="input"
          value={role}
          onChange={(e) => setRole(e.target.value as OrgRole)}
        >
          {ROLES.map((r) => (
            <option key={r.value} value={r.value}>
              {r.label}
            </option>
          ))}
        </select>
      </div>
      <button type="submit" className="btn btn-brand" disabled={add.isPending}>
        {add.isPending ? "Adding…" : "Add member"}
      </button>
      {error && (
        <div className="w-full text-[13px] text-danger" role="alert">
          {error}
        </div>
      )}
    </form>
  );
}

function MemberRow({
  member,
  orgId,
  canManage,
  isSelf,
}: {
  member: Member;
  orgId: string;
  canManage: boolean;
  isSelf: boolean;
}) {
  const { setRole, remove } = useMemberMutations(orgId);
  const busy = setRole.isPending || remove.isPending;

  return (
    <tr className="row">
      <td className="td">
        <div className="font-medium">{member.full_name || "—"}</div>
        <div className="text-xs text-muted">{member.email}</div>
      </td>
      <td className="td">
        {canManage && !isSelf ? (
          <select
            className="input py-1 text-[13px]"
            value={member.role}
            disabled={busy}
            onChange={(e) =>
              setRole.mutate({
                userId: member.user_id,
                role: e.target.value as OrgRole,
              })
            }
          >
            {ROLES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        ) : (
          <RoleBadge role={member.role} />
        )}
      </td>
      <td className="td text-muted">{relativeTime(member.joined_at)}</td>
      <td className="td text-right">
        {isSelf ? (
          <Badge className="bg-brand/15 text-brand">you</Badge>
        ) : canManage ? (
          <button
            className="btn btn-danger"
            disabled={busy}
            onClick={() => {
              if (
                window.confirm(
                  `Remove ${member.email} from this organization?`,
                )
              ) {
                remove.mutate(member.user_id);
              }
            }}
          >
            Remove
          </button>
        ) : null}
      </td>
    </tr>
  );
}

export default function TeamPage() {
  const { me, project, role, can } = useAuth();
  const orgId = project?.org_id ?? null;
  const members = useMembers(orgId);
  const canManage = can("admin");

  return (
    <div>
      <PageHeader
        title="Team"
        description="Who can reach this organization, and what each of them may do."
        actions={role ? <RoleBadge role={role} prefix="You are" /> : undefined}
      />

      {canManage ? (
        <Card className="mb-4 p-4">
          <InviteForm orgId={orgId!} />
          <p className="mt-3 text-xs text-muted">
            The person must already have an account — registration is
            self-service at{" "}
            <span className="mono">/register</span>. Adding them here grants
            access to every project in this organization.
          </p>
        </Card>
      ) : (
        <Card className="mb-4 p-4 text-[13px] text-muted">
          Managing members requires the <span className="mono">admin</span>{" "}
          role. You can see the roster below.
        </Card>
      )}

      <Card>
        {members.isLoading ? (
          <Spinner />
        ) : !members.data || members.data.length === 0 ? (
          <EmptyState>No members yet.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Person</th>
                  <th className="th">Role</th>
                  <th className="th">Joined</th>
                  <th className="th text-right" />
                </tr>
              </thead>
              <tbody>
                {members.data.map((m) => (
                  <MemberRow
                    key={m.user_id}
                    member={m}
                    orgId={orgId!}
                    canManage={canManage}
                    isSelf={m.user_id === me?.user.id}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card className="mt-4 p-4">
        <div className="mb-3 text-[13px] font-semibold">What each role may do</div>
        <div className="grid gap-2 sm:grid-cols-2">
          {ROLES.map((r) => (
            <div key={r.value} className="flex items-start gap-2.5">
              <RoleBadge role={r.value} />
              <span className="text-[13px] text-muted">{r.blurb}</span>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-faint">
          Roles are ranked, so a check for <span className="mono">member</span>{" "}
          is satisfied by <span className="mono">admin</span> and{" "}
          <span className="mono">owner</span>. Hidden controls are a courtesy —
          every action is checked again on the server.
        </p>
      </Card>
    </div>
  );
}
