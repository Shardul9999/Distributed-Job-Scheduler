import type { Config } from "tailwindcss";

// Palette taken from codity.ai's own stylesheet, not approximated by eye.
//
// Surfaces and type are their marketing-site tokens (`--paper` #fafafe,
// `--paper-raised`, `--paper-sunk`, and the `--ink` scale), the accent is their
// `--accent` indigo, and the sidebar carries their signature deep-violet
// gradient. Status colours come from Codity's own console in *light* mode --
// the marketing site has no error/success palette, and these are tuned for
// contrast on a near-white ground, which their brand violets are not.
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
        bg: "#fafafe", // --paper
        panel: "#fefdff", // --paper-raised  (cards)
        head: "#f2f2f9", // --paper-sunk    (table headers, wells)
        inset: "#f2f2f9",
        panel2: "#f2f2f9", // inputs, secondary buttons
        raised: "#e9e9f2", // their hover state

        // Hairlines, derived to sit between paper and paper-sunk.
        border: "#e3e3ee",
        subtle: "#eeeef5",
        strong: "#c9c9d8",

        // Type -- the --ink scale.
        fg: "#1b1e2e", // --ink
        muted: "#6e717e", // --ink-3
        faint: "#9698a2", // --ink-4

        // Codity indigo. Deepens on press rather than lightening, which is how
        // their own CTAs behave.
        brand: "#5055d3", // --accent
        "brand-hover": "#4335a8", // --accent-deep
        "brand-active": "#3a2f92",

        // Their deeper brand tones, used for the sidebar gradient and charts.
        "brand-violet": "#421f7f",
        "brand-mid": "#3b3db5",
        "brand-blue": "#0074d8",
        "brand-ink": "#0e172f",

        // State. Legible on near-white; never decorative.
        ok: "#12885a",
        danger: "#d13b30",
        warn: "#b5711a",
        info: "#0074d8",
        neutral: "#5f5f5f",

        // Sidebar ink, for text sitting on the violet gradient.
        "on-violet": "#f4f3fb",
        "on-violet-muted": "#b9b4d8",
      },
      backgroundImage: {
        // codity.ai's hero/section gradient, verbatim.
        "brand-gradient":
          "linear-gradient(100deg,#2c0d56 0%,#281060 46%,#1a1869 100%)",
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
