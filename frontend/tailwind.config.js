/** Caligo design tokens. Colours resolve to CSS variables (src/index.css) so there is one source of truth. */
const token = (name) => `rgb(var(--c-${name}) / <alpha-value>)`;
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        parchment: token("parchment"), paper: token("paper"), stone: token("stone"), ink: token("ink"), "ink-soft": token("ink-soft"),
        terracotta: token("terracotta"), "terracotta-deep": token("terracotta-deep"), "terracotta-wash": token("terracotta-wash"),
        sage: token("sage"), "sage-deep": token("sage-deep"), "sage-wash": token("sage-wash"),
      },
      fontFamily: {
        display: ['"Newsreader"', "Georgia", "serif"],
        sans: ['"Inter Variable"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      fontSize: { xs: ["0.8125rem", "1.25rem"], sm: ["0.9375rem", "1.5rem"], base: ["1rem", "1.6rem"] },
      borderRadius: { DEFAULT: "2px", md: "3px", lg: "4px" },
      maxWidth: { prose: "68ch" },
    },
  },
  plugins: [],
};
