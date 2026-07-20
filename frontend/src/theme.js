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
  primaryShade: { light: 6, dark: 8 },
  defaultRadius: 'md',
  fontFamily:
    'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  headings: { fontWeight: '700' },
})
