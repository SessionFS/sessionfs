import { defineConfig } from '@playwright/test';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));

/** Where screenshots and report.json land. Override with SFS_VISUAL_OUT. */
export const OUT_DIR = process.env.SFS_VISUAL_OUT || resolve(here, '.output', 'screens');
const PORT = Number(process.env.SFS_VISUAL_PORT || 5199);

process.env.SFS_VISUAL_OUT = OUT_DIR;
process.env.SFS_VISUAL_PORT = String(PORT);

export default defineConfig({
  testDir: here,
  // .pw.ts keeps vitest (which globs *.spec.ts) from picking these up.
  testMatch: '*.pw.ts',
  outputDir: resolve(here, '.output', 'pw'),
  fullyParallel: true,
  workers: Number(process.env.SFS_VISUAL_WORKERS || 4),
  retries: 0,
  reporter: [['list']],
  timeout: 45_000,
  globalSetup: resolve(here, 'global-setup.ts'),
  globalTeardown: resolve(here, 'global-teardown.ts'),
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    browserName: 'chromium',
  },
  webServer: {
    command: `npx vite --host 127.0.0.1 --port ${PORT} --strictPort`,
    cwd: resolve(here, '..'),
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
