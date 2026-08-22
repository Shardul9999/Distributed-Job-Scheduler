import type { Config } from "tailwindcss";

// Palette and shape are taken from Codity's own product dashboard
// (dashboard.codity.ai) so this tool reads as part of the same family: near-black
// layered surfaces, a single blue accent reserved for actions, and colour used
// only to carry state -- green healthy, amber waiting, red failed.
//
// The values below are Codity's design tokens verbatim (their `--bg-*`,
// `--border-*`, `--text-*`, `--accent*` and `--status-*` custom properties in
// dark mode), not an approximation by eye.
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Surfaces, darkest to lightest: page, card, raised control.
        bg: "#090909", // --bg-app
        panel: "#0d0d0d", // --bg-surface
        head: "#121212", // --bg-head   (table headers, sidebar)
        inset: "#131313", // --bg-inset  (code blocks, wells)
        panel2: "#191919", // --bg-raised (buttons, inputs)
        raised: "#222222", // --bg-raised-hover

        // Hairlines. `border` is the default; the other two are deliberate.
        border: "#202020",
        subtle: "#1a1a1a",
        strong: "#303030",

        // Type.
        fg: "#ededed", // --text-primary
        muted: "#8e8e8e", // --text-secondary
        faint: "#7d7d7d", // --text-tertiary

        // One accent, used only for primary actions and active nav.
        brand: "#0075ff", // --accent
        "brand-hover": "#2f8eff", // --accent-hover
        "brand-active": "#0063db", // --accent-active

        // State. Never decorative.
        ok: "#3ec98a", // --status-success
        danger: "#ff5b52", // --status-error
        warn: "#f5a83c", // --status-warning
        info: "#2f8eff",
        neutral: "#8e8e8e", // --status-neutral
      },
      // Codity's radii are tight (3-6px). Sharp corners read as precise and
      // technical, which is the register an operations console wants.
      borderRadius: {
        sm: "3px",
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
        // Codity's --ease-out-expo.
        expo: "cubic-bezier(.16, 1, .3, 1)",
      },
    },
  },
  plugins: [],
};

export default config;
