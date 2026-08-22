"use client";

import type { ReactElement } from "react";
import { useTheme, type ThemePreference } from "@/lib/theme";
import { IconMoon, IconSun, IconSystem } from "./icons";

const OPTIONS: {
  value: ThemePreference;
  label: string;
  Icon: (p: { className?: string }) => ReactElement;
}[] = [
  { value: "light", label: "Light", Icon: IconSun },
  { value: "dark", label: "Dark", Icon: IconMoon },
  { value: "system", label: "System", Icon: IconSystem },
];

/**
 * Segmented control rather than a two-state switch.
 *
 * A plain toggle cannot express "follow the OS", which is the setting most
 * people actually want -- and once you flip a binary switch there is no way
 * back to automatic without clearing site data. Three explicit options cost one
 * extra button and remove that dead end.
 *
 * Lives in the sidebar, so it renders on the violet gradient in both themes;
 * the colours here are fixed white-alphas for that reason rather than theme
 * tokens.
 */
export function ThemeToggle() {
  const { preference, setPreference } = useTheme();

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className="flex items-center gap-0.5 rounded border border-white/15 bg-white/10 p-0.5"
    >
      {OPTIONS.map(({ value, label, Icon }) => {
        const active = preference === value;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            title={`${label} theme`}
            onClick={() => setPreference(value)}
            className={`flex flex-1 items-center justify-center rounded-sm py-1 transition-colors duration-150 ${
              active
                ? "bg-white/20 text-white"
                : "text-on-violet-muted hover:bg-white/10 hover:text-on-violet"
            }`}
          >
            <Icon className="h-[13px] w-[13px]" />
            <span className="sr-only">{label}</span>
          </button>
        );
      })}
    </div>
  );
}
