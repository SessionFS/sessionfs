import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Explicit manualChunks — same bytes ship as Vite's automatic chunking,
// but the chunk filenames in dist/assets/ are now self-documenting
// instead of being named after whichever small file happened to share
// their dependency cluster (e.g. the "FieldError-*.js" chunk that was
// actually the zod library).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (id.includes('/zod/')) return 'zod'
          if (id.includes('/react-markdown/') || id.includes('/remark-') || id.includes('/rehype-') || id.includes('/micromark') || id.includes('/mdast-') || id.includes('/unist-') || id.includes('/hast-')) return 'markdown'
          if (id.includes('/@tanstack/react-query')) return 'react-query'
          if (id.includes('/react-router')) return 'react-router'
          if (id.includes('/react-dom/') || id.includes('/react/') || id.includes('/scheduler/')) return 'react-vendor'
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
  },
})
