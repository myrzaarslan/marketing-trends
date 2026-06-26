import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Proxy API calls to FastAPI during development
      '/digest': 'http://localhost:8001',
      '/post': 'http://localhost:8001',
      '/media': 'http://localhost:8001',
      '/refresh': 'http://localhost:8001',
      '/thumbnails': 'http://localhost:8001',
      '/health': 'http://localhost:8001',
    },
  },
})
