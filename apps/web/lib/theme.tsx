"use client";

// Theme state, persisted per browser.
//
// Three-valued on purpose: "system" is a real choice, not the absence of one.
// A user who has their OS on a schedule expects the dashboard to follow it, and
// collapsing that to a plain light/dark boolean silently opts them out.
//
// The resolved theme is written to `data-theme` on <html>, which is the same
// mechanism Codity's own console uses, and every colour token in globals.css
// keys off that attribute.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "codity-theme";

type ThemeContextValue = {
  /** What the user chose, including "system". */
  preference: ThemePreference;
  /** What is actually on screen right now. */
  theme: ResolvedTheme;
  setPreference: (p: ThemePreference) => void;
  /** Flips to the opposite of what is currently displayed. */
  toggle: () => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

function systemTheme(): ResolvedTheme {
  if (typeof window === "undefined") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function apply(theme: ResolvedTheme) {
  document.documentElement.setAttribute("data-theme", theme);
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // Initialised to "system" on both server and client so the first client
  // render matches the server's HTML. The inline script in layout.tsx has
  // already painted the correct theme by this point; the effect below syncs
  // React's state up to it without ever causing a hydration mismatch.
  const [preference, setPreferenceState] = useState<ThemePreference>("system");
  const [theme, setTheme] = useState<ResolvedTheme>("light");

  useEffect(() => {
    let stored: ThemePreference | null = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY) as ThemePreference | null;
    } catch {
      // Private mode, or site data blocked. Fall through to "system".
    }
    const pref: ThemePreference =
      stored === "light" || stored === "dark" || stored === "system"
        ? stored
        : "system";
    const resolved = pref === "system" ? systemTheme() : pref;
    setPreferenceState(pref);
    setTheme(resolved);
    apply(resolved);
    // Enables colour transitions only after the first paint, so the initial
    // render does not visibly fade in from the wrong palette.
    document.documentElement.classList.add("theme-ready");
  }, []);

  // Follow the OS while the preference is "system" -- including a change made
  // after the page is already open.
  useEffect(() => {
    if (preference !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      const resolved = systemTheme();
      setTheme(resolved);
      apply(resolved);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [preference]);

  const setPreference = useCallback((p: ThemePreference) => {
    const resolved = p === "system" ? systemTheme() : p;
    setPreferenceState(p);
    setTheme(resolved);
    apply(resolved);
    try {
      localStorage.setItem(STORAGE_KEY, p);
    } catch {
      // Persistence is a convenience, not a requirement -- the theme still
      // applies for this session.
    }
  }, []);

  const toggle = useCallback(() => {
    // Deliberately flips against what is on screen, not against the stored
    // preference: from "system" resolving to dark, one click should give light.
    setPreference(
      (document.documentElement.getAttribute("data-theme") ?? "light") === "dark"
        ? "light"
        : "dark",
    );
  }, [setPreference]);

  const value = useMemo(
    () => ({ preference, theme, setPreference, toggle }),
    [preference, theme, setPreference, toggle],
  );

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside ThemeProvider");
  return ctx;
}

/**
 * Runs before first paint to stamp `data-theme` on <html>.
 *
 * Without this the page renders in the default palette and then snaps to the
 * stored one a frame later -- the flash-of-wrong-theme every themed app has to
 * solve. It has to be inline and synchronous in <head>; a React effect is far
 * too late.
 */
export const themeInitScript = `
(function(){
  try {
    var p = localStorage.getItem(${JSON.stringify(STORAGE_KEY)});
    var t = (p === 'light' || p === 'dark')
      ? p
      : (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
`;
