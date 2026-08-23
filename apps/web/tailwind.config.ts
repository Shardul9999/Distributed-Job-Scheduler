import type { Config } from "tailwindcss";

// Colours resolve through the CSS custom properties defined in app/globals.css
// -- Codity's product console in dark mode. Each is written as
// `rgb(var(--c-x) / <alpha-value>)` rather than a flat var so Tailwind's
// opacity modifiers keep working: the status badges use `bg-ok/15`,
// `bg-danger/20` and friends throughout.
const c = (name: string) => `rgb(var(--c-${name}) / <alpha-value>)`;

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Surfaces, from page to raised control.
        bg: c("bg"),
        panel: c("panel"),
        head: c("head"),
        inset: c("inset"),
        panel2: c("panel2"),
        raised: c("raised"),

        // Hairlines.
        border: c("border"),
        subtle: c("subtle"),
        strong: c("strong"),

        // Type.
        fg: c("fg"),
        muted: c("muted"),
        faint: c("faint"),

        // Codity indigo.
        brand: c("brand"),
        "brand-hover": c("brand-hover"),
        "brand-active": c("brand-active"),

        // State. Never decorative.
        ok: c("ok"),
        danger: c("danger"),
        warn: c("warn"),
        info: c("info"),

        // Fixed brand tones, used for accents that must not shift with a
        // surface token.
        "brand-violet": "#421f7f",
        "brand-mid": "#3b3db5",
        "brand-blue": "#0074d8",
      },
      // Codity's radii are tight (0-6px). Sharp corners read as precise.
      borderRadius: {
        sm: "2px",
        DEFAULT: "4px",
        md: "4px",
        lg: "6px",
        xl: "6px",
        "2xl": "8px",
      },
      fontFamily: {
        sans: ["var(--font-archivo)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-jetbrains)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      transitionTimingFunction: {
        expo: "cubic-bezier(.16, 1, .3, 1)", // their --ease-out-expo
      },
    },
  },
  plugins: [],
};

export default config;
