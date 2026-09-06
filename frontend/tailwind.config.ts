import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      colors: {
        ink: {
          950: "#07090d", 900: "#0b0e14", 850: "#11151d",
          800: "#161b25", 700: "#1f2632", 600: "#2b3442",
          500: "#3d4859", 400: "#5b6779", 300: "#8792a5",
          200: "#b6bfcd", 100: "#dde3ec",
        },
        signal: { DEFAULT: "#4cc9f0", dim: "#2a7f9e" },
        warn: "#f4a261",
        danger: "#ef476f",
        good: "#06d6a0",
        muted: "#8792a5",
      },
      keyframes: {
        pulseRing: {
          "0%": { transform: "scale(0.9)", opacity: "0.8" },
          "100%": { transform: "scale(1.6)", opacity: "0" },
        },
        slideIn: {
          from: { opacity: "0", transform: "translateY(-4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        pulseRing: "pulseRing 1.6s ease-out infinite",
        slideIn: "slideIn 180ms ease-out",
      },
    },
  },
  plugins: [],
} satisfies Config;
