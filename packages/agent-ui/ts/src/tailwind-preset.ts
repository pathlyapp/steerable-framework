/**
 * Steerable Tailwind preset.
 *
 * Exposes design tokens (CSS variables) and a small theme extension covering
 * the colors, spacing, and animations used by `@steerable/agent-ui` components.
 *
 * Usage:
 *
 *   // tailwind.config.ts
 *   import preset from '@steerable/agent-ui/tailwind-preset';
 *   export default { presets: [preset], content: ['./src/**\/*.{ts,tsx}'] };
 *
 * Override any token in your project's `globals.css`:
 *
 *   :root {
 *     --agent-canvas: 0 0% 100%;
 *     --agent-foreground: 240 10% 3.9%;
 *     --agent-accent: 217 91% 60%;
 *   }
 *   .dark {
 *     --agent-canvas: 240 10% 3.9%;
 *     --agent-foreground: 0 0% 98%;
 *   }
 */

const preset = {
  // Tell Tailwind we use the `class` darkmode toggle so consumers can opt-in.
  darkMode: 'class' as const,
  theme: {
    extend: {
      colors: {
        'agent-canvas': 'hsl(var(--agent-canvas) / <alpha-value>)',
        'agent-foreground': 'hsl(var(--agent-foreground) / <alpha-value>)',
        'agent-muted': 'hsl(var(--agent-muted) / <alpha-value>)',
        'agent-muted-foreground':
          'hsl(var(--agent-muted-foreground) / <alpha-value>)',
        'agent-border': 'hsl(var(--agent-border) / <alpha-value>)',
        'agent-accent': 'hsl(var(--agent-accent) / <alpha-value>)',
        'agent-accent-foreground':
          'hsl(var(--agent-accent-foreground) / <alpha-value>)',
        'agent-destructive':
          'hsl(var(--agent-destructive) / <alpha-value>)',
        'agent-tool-read': 'hsl(var(--agent-tool-read) / <alpha-value>)',
        'agent-tool-write': 'hsl(var(--agent-tool-write) / <alpha-value>)',
        'agent-tool-destructive':
          'hsl(var(--agent-tool-destructive) / <alpha-value>)',
      },
      borderRadius: {
        'agent-sm': 'calc(var(--agent-radius) - 4px)',
        'agent-md': 'calc(var(--agent-radius) - 2px)',
        'agent-lg': 'var(--agent-radius)',
      },
      animation: {
        'agent-cursor-blink': 'agent-cursor-blink 1s steps(1) infinite',
        'agent-shimmer': 'agent-shimmer 2s linear infinite',
      },
      keyframes: {
        'agent-cursor-blink': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        'agent-shimmer': {
          '0%': { backgroundPosition: '-1000px 0' },
          '100%': { backgroundPosition: '1000px 0' },
        },
      },
    },
  },
  // Default token values; consumers can override in their own CSS.
  // The defaults below satisfy WCAG 2 AA color-contrast (≥ 4.5:1 for normal
  // text) against `--agent-canvas` (light) and `--agent-muted`. Override at
  // your own risk — the framework's Storybook a11y suite will start failing
  // if a regression is introduced.
  plugins: [
    function injectDefaultTokens(api: { addBase: (rules: Record<string, unknown>) => void }) {
      api.addBase({
        ':root': {
          '--agent-canvas': '0 0% 100%',
          '--agent-foreground': '240 10% 3.9%',
          '--agent-muted': '240 4.8% 95.9%',
          '--agent-muted-foreground': '240 5% 32%',
          '--agent-border': '240 5.9% 90%',
          '--agent-accent': '217 91% 42%',
          '--agent-accent-foreground': '0 0% 100%',
          '--agent-destructive': '0 75% 42%',
          '--agent-tool-read': '142 71% 22%',
          '--agent-tool-write': '28 90% 28%',
          '--agent-tool-destructive': '0 75% 42%',
          '--agent-radius': '0.75rem',
        },
        '.dark': {
          '--agent-canvas': '240 10% 3.9%',
          '--agent-foreground': '0 0% 98%',
          '--agent-muted': '240 3.7% 15.9%',
          '--agent-muted-foreground': '240 5% 75%',
          '--agent-border': '240 3.7% 15.9%',
          '--agent-accent': '217 91% 65%',
          '--agent-accent-foreground': '240 10% 3.9%',
          '--agent-destructive': '0 75% 60%',
          '--agent-tool-read': '142 71% 60%',
          '--agent-tool-write': '38 92% 60%',
          '--agent-tool-destructive': '0 75% 60%',
        },
      });
    },
  ],
};

export default preset;
