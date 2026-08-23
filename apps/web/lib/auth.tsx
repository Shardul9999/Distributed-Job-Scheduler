"use client";

// Auth + tenant context. Holds the current user, the selected organization and
// project, and persists the project choice so a refresh lands back where you
// were. Every project-scoped page reads `projectId` from here.

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useRouter } from "next/navigation";
import { api, getToken, setToken } from "./api";
import type { Me, Organization, OrgRole, Project } from "./types";

/** Privilege ordering, mirroring `_ROLE_RANK` in `apps/api/core/deps.py`.
 *
 *  The client copy exists to decide what to *render*; the server copy decides
 *  what is *permitted*. They must agree, but the UI one is a courtesy -- hiding
 *  a button is not a security boundary, and every guarded action is refused
 *  again server-side. */
const RANK: Record<OrgRole, number> = {
  viewer: 0,
  member: 1,
  admin: 2,
  owner: 3,
};

const PROJECT_KEY = "codity.project_id";

interface AuthState {
  ready: boolean;
  me: Me | null;
  orgs: Organization[];
  projects: Project[];
  projectId: string | null;
  project: Project | null;
  /** The caller's role in the organization owning the selected project. */
  role: OrgRole | null;
  /** True when that role meets `minimum`. Used to gate write controls. */
  can: (minimum: OrgRole) => boolean;
  setProjectId: (id: string) => void;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [orgs, setOrgs] = useState<Organization[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectIdState] = useState<string | null>(null);

  async function bootstrap() {
    const token = getToken();
    if (!token) {
      setReady(true);
      return;
    }
    try {
      const [meRes, orgRes] = await Promise.all([api.me(), api.orgs()]);
      setMe(meRes);
      setOrgs(orgRes);
      // Collect projects across every org the user belongs to, so the switcher
      // is not silently scoped to whichever org happens to be first.
      const projLists = await Promise.all(orgRes.map((o) => api.projects(o.id)));
      const allProjects = projLists.flat();
      setProjects(allProjects);

      const stored =
        typeof window !== "undefined"
          ? window.localStorage.getItem(PROJECT_KEY)
          : null;
      const initial =
        allProjects.find((p) => p.id === stored)?.id ??
        allProjects[0]?.id ??
        null;
      setProjectIdState(initial);
    } catch {
      // Token invalid/expired: drop it and let the guard redirect.
      setToken(null);
    } finally {
      setReady(true);
    }
  }

  useEffect(() => {
    bootstrap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function setProjectId(id: string) {
    setProjectIdState(id);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(PROJECT_KEY, id);
    }
  }

  async function login(email: string, password: string) {
    const tok = await api.login(email, password);
    setToken(tok.access_token);
    await bootstrap();
    router.push("/");
  }

  function logout() {
    setToken(null);
    setMe(null);
    setOrgs([]);
    setProjects([]);
    setProjectIdState(null);
    router.push("/login");
  }

  const value = useMemo<AuthState>(() => {
    const project = projects.find((p) => p.id === projectId) ?? null;
    // Role is a property of the organization, and the project switcher spans
    // every org the user belongs to -- so it is resolved from the *selected*
    // project's org rather than read once at login.
    const role =
      me?.organizations.find((o) => o.org_id === project?.org_id)?.role ?? null;

    return {
      ready,
      me,
      orgs,
      projects,
      projectId,
      project,
      role,
      can: (minimum) => (role === null ? false : RANK[role] >= RANK[minimum]),
      setProjectId,
      login,
      logout,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, me, orgs, projects, projectId]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
