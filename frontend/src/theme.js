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
  spacing: {
    xs: '0.5rem',
    sm: '0.75rem',
    md: '1rem',
    lg: '1.5rem',
    xl: '2rem',
  },
  fontFamily:
    'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  headings: {
    fontWeight: '800',
    sizes: {
      h1: { fontSize: '2rem', lineHeight: '1.15' },
      h2: { fontSize: '1.5rem', lineHeight: '1.2' },
      h3: { fontSize: '1.25rem', lineHeight: '1.25' },
      h4: { fontSize: '1rem', lineHeight: '1.3' },
    },
  },
  defaultGradient: { from: 'fresh.5', to: 'fresh.6', deg: 135 },
})
