import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

// Resolution order:
//   1. SFS_VERSION env (CI / Docker build-arg / explicit override)
//   2. ./VERSION file shipped alongside the dashboard (written by
//      /release; visible inside Vercel's build container)
//   3. ../pyproject.toml (local dev convenience when the repo root is
//      reachable — not the case inside Vercel or Docker builds)
//   4. 'dev' sentinel
function resolveAppVersion(): string {
  if (process.env.SFS_VERSION) return process.env.SFS_VERSION
  const here = dirname(fileURLToPath(import.meta.url))
  try {
    return readFileSync(resolve(here, 'VERSION'), 'utf8').trim()
  } catch {
    // fall through
  }
  try {
    const pyproject = readFileSync(resolve(here, '..', 'pyproject.toml'), 'utf8')
    const m = pyproject.match(/^version\s*=\s*"([^"]+)"/m)
    if (m) return m[1]
  } catch {
    // fall through
  }
  return 'dev'
}

const APP_VERSION = resolveAppVersion()

// Explicit manualChunks — same bytes ship as Vite's automatic chunking,
// but the chunk filenames in dist/assets/ are now self-documenting
// instead of being named after whichever small file happened to share
// their dependency cluster (e.g. the "FieldError-*.js" chunk that was
// actually the zod library).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
  },
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
