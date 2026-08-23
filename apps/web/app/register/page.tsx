"use client";

// Self-service sign-up. `POST /auth/register` creates the user *and* their first
// organization and returns a token pair in one response, so there is no separate
// "create your org" step to land on afterwards.

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, api, getToken, setToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function RegisterPage() {
  const router = useRouter();
  const { ready, me } = useAuth();
  const [fullName, setFullName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (ready && me && getToken()) router.replace("/");
  }, [ready, me, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const tokens = await api.register({
        email,
        password,
        full_name: fullName,
        organization_name: orgName,
      });
      setToken(tokens.access_token);
      // Full reload rather than a client push: the auth context bootstraps once
      // on mount, and it needs to re-read /auth/me with the new token.
      window.location.href = "/";
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Sign up failed. Try again.",
      );
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="card w-full max-w-sm p-7">
        <div className="mb-6 flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand font-bold text-white">
            C
          </span>
          <div>
            <div className="font-semibold">Create an account</div>
            <div className="text-xs text-muted">
              You become the owner of a new organization
            </div>
          </div>
        </div>

        <form onSubmit={onSubmit} className="space-y-3">
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wide text-muted">
              Full name
            </label>
            <input
              className="input w-full"
              autoComplete="name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wide text-muted">
              Organization
            </label>
            <input
              className="input w-full"
              placeholder="Acme Inc"
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wide text-muted">
              Email
            </label>
            <input
              className="input w-full"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-wide text-muted">
              Password
            </label>
            <input
              className="input w-full"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          {error && (
            <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
              {error}
            </div>
          )}

          <button type="submit" className="btn btn-brand w-full" disabled={busy}>
            {busy ? "Creating…" : "Create account"}
          </button>
        </form>

        <p className="mt-4 text-center text-[13px] text-muted">
          Already have an account?{" "}
          <Link href="/login" className="text-brand hover:underline">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
