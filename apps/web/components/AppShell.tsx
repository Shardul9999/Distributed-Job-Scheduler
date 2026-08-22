"use client";

// Wraps every authenticated page: the auth guard, the sidebar, and a single SSE
// subscription shared downward through context. One EventSource for the whole
// app -- the overview reads the same stream that drives the sidebar's live dot,
// rather than each opening its own.

import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { getToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useLiveSnapshot } from "@/lib/sse";
import type { LiveSnapshot } from "@/lib/types";
import { Sidebar } from "./Sidebar";
import { Spinner } from "./ui";

interface LiveCtx {
  snapshot: LiveSnapshot | null;
  connected: boolean;
}
const Live = createContext<LiveCtx>({ snapshot: null, connected: false });
export const useLive = () => useContext(Live);

export function AppShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { ready, me } = useAuth();
  const { snapshot, connected } = useLiveSnapshot(2);

  useEffect(() => {
    if (ready && (!getToken() || !me)) {
      router.replace("/login");
    }
  }, [ready, me, router]);

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (!me) return null; // redirecting

  return (
    <Live.Provider value={{ snapshot, connected }}>
      <div className="flex">
        <Sidebar connected={connected} />
        <main className="h-screen flex-1 overflow-y-auto">
          <div className="mx-auto max-w-7xl px-8 py-7">{children}</div>
        </main>
      </div>
    </Live.Provider>
  );
}
