import type { Config } from "tailwindcss";

// A small, deliberate palette. The dashboard is an operator tool, so the
// surface colours stay dark and low-contrast and colour is reserved for state:
// green for healthy, amber for waiting, red for failure, sky for in-flight.
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "#0b0e14",
        panel: "#12161f",
        panel2: "#171c28",
        border: "#232a39",
        muted: "#8b94a7",
        fg: "#e6e9f0",
        brand: "#5b8cff",
        ok: "#3ecf8e",
        warn: "#f5b544",
        danger: "#f2555a",
        info: "#56b6ff",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
