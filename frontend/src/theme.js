import { createTheme } from '@mantine/core'

// Fresh-green palette (10 shades, light -> dark) matching the HolaFresca hero.
const fresh = [
  '#e9f9f0',
  '#d3f0e0',
  '#a6e0c1',
  '#75cf9f',
  '#4fc184',
  '#37b972',
  '#26b268',
  '#159c57',
  '#028b4b',
  '#00783d',
]

export const theme = createTheme({
  primaryColor: 'fresh',
  colors: { fresh },
  primaryShade: { light: 6, dark: 5 },
  defaultRadius: 'lg',
  fontFamily:
    'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  headings: {
    fontWeight: '800',
    sizes: {
      h1: { fontSize: 'clamp(2.35rem, 5vw, 4.1rem)', lineHeight: '0.98' },
      h2: { fontSize: 'clamp(2rem, 3vw, 3rem)', lineHeight: '1.05' },
    },
  },
  defaultGradient: { from: 'fresh.5', to: 'fresh.6', deg: 135 },
})
