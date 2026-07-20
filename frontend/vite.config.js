import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend port is overridable via VITE_API_TARGET so the dev server can
// point at a non-default port when 8000 is taken.
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': apiTarget,
    },
  },
})
