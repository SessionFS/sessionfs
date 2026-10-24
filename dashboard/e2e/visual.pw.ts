import { test } from '@playwright/test';
import { appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { mockApi } from './fixtures/mock-api';
import { PROJECT_ID } from './fixtures/data';

const require = createRequire(import.meta.url);
const AXE_PATH = require.resolve('axe-core/axe.min.js');

const OUT = process.env.SFS_VISUAL_OUT!;
const PORT = process.env.SFS_VISUAL_PORT!;
const BASE_URL = `http://127.0.0.1:${PORT}`;

// Obviously fake: the API is fully intercepted, nothing here is a credential.
const FAKE_KEY = 'sk_sfs_FAKE_VISUAL_TEST_KEY_0000000000';

// Project pages are routed by id (a legacy remote URL redirects to it).
const PROJECT = PROJECT_ID;

const PAGES: { name: string; path: string }[] = [
  { name: 'sessions', path: '/' },
  { name: 'projects', path: '/projects' },
  { name: 'project-overview', path: `/projects/${PROJECT}` },
  { name: 'project-knowledge', path: `/projects/${PROJECT}?tab=knowledge` },
  { name: 'project-work', path: `/projects/${PROJECT}?tab=work` },
  { name: 'project-rules-skills', path: `/projects/${PROJECT}?tab=config` },
  { name: 'project-settings', path: `/projects/${PROJECT}?tab=settings` },
  { name: 'handoffs', path: '/handoffs' },
  { name: 'marketplace', path: '/marketplace' },
  { name: 'org-members', path: '/settings/organization?tab=members' },
  { name: 'org-security', path: '/settings/organization?tab=security' },
  { name: 'settings-account', path: '/settings/account' },
  { name: 'settings-preferences', path: '/settings/preferences' },
  { name: 'settings-integrations', path: '/settings/integrations' },
  { name: 'settings-data-protection', path: '/settings/data-protection' },
  { name: 'settings-sync', path: '/settings/sync' },
  { name: 'help', path: '/help' },
  { name: 'billing', path: '/settings/billing' },
];

const THEMES = ['dark', 'light'] as const;
const WIDTHS = [375, 768, 1280];

for (const theme of THEMES) {
  for (const width of WIDTHS) {
    test.describe(`${theme} ${width}`, () => {
      test.use({ viewport: { width, height: 900 }, colorScheme: theme });

      for (const pg of PAGES) {
        test(pg.name, async ({ page }) => {
          const misses = await mockApi(page);
          const errors: string[] = [];
          page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
          page.on('console', (m) => {
            if (m.type() === 'error') errors.push(`console: ${m.text()}`);
          });

          await page.addInitScript(
            ({ key, base, theme }) => {
              try {
                sessionStorage.setItem('sessionfs_auth', JSON.stringify({ apiKey: key, baseUrl: base }));
                localStorage.setItem('sfs-theme', theme);
              } catch {
                /* storage unavailable */
              }
            },
            { key: FAKE_KEY, base: BASE_URL, theme },
          );

          await page.goto(pg.path, { waitUntil: 'networkidle' });
          // Lazy chunks and react-query settle after the network goes quiet.
          await page.waitForTimeout(400);

          const file = `${pg.name}__${theme}__${width}.png`;
          await page.screenshot({ path: join(OUT, file), fullPage: true });

          const metrics = await page.evaluate(() => {
            const el = document.scrollingElement!;
            const text = document.body.innerText.trim();
            return {
              scrollWidth: el.scrollWidth,
              innerWidth: window.innerWidth,
              textLength: text.length,
              hasBoundary: /something went wrong|unexpected error|failed to load/i.test(text),
              loginShown: location.pathname === '/login',
              sample: text.slice(0, 80).replace(/\s+/g, ' '),
            };
          });

          // document scrollWidth misses content clipped by an overflow-hidden ancestor, so
          // also look for visible elements whose right edge is past the viewport.
          const clipped = await page.evaluate(() => {
            const vw = window.innerWidth;
            const out: string[] = [];
            const describe = (el: Element) =>
              `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${
                typeof el.className === 'string' && el.className
                  ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.')
                  : ''
              }`;
            const inScroller = (el: Element) => {
              for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
                const o = getComputedStyle(p).overflowX;
                if ((o === 'auto' || o === 'scroll') && p.scrollWidth > p.clientWidth) return true;
              }
              return false;
            };
            for (const el of Array.from(document.body.querySelectorAll('*'))) {
              const r = el.getBoundingClientRect();
              if (r.width === 0 || r.height === 0 || r.right <= vw + 1) continue;
              const cs = getComputedStyle(el);
              if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
              if (r.left >= vw || el.closest('[aria-hidden="true"], .sr-only')) continue;
              if (inScroller(el) || cs.pointerEvents === 'none') continue;
              out.push(`${describe(el)} right=${Math.round(r.right)}`);
            }
            return out.slice(0, 8);
          });

          let broken: string | null = null;
          if (metrics.loginShown) broken = 'redirected to /login (auth seed not honoured)';
          else if (metrics.textLength < 40) broken = `near-blank page (${metrics.textLength} chars)`;
          else if (metrics.hasBoundary) broken = `error text on page: "${metrics.sample}"`;

          await page.addScriptTag({ path: AXE_PATH });
          const axe = await page.evaluate(async () => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const res = await (window as any).axe.run(document, { resultTypes: ['violations'] });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            return res.violations.map((v: any) => ({
              id: v.id,
              impact: v.impact,
              nodes: v.nodes.length,
              targets: v.nodes
                .slice(0, 3)
                .map((n: any) => String(n.target.join(' ')).slice(0, 120)),
              detail: v.nodes
                .slice(0, 12)
                .map((n: any) => `${String(n.target.join(' ')).slice(0, 70)} :: ${(n.any[0]?.message || n.failureSummary || '').slice(0, 150)}`),
            }));
          });

          const row = {
            page: pg.name,
            theme,
            width,
            file,
            overflowPx: Math.max(0, metrics.scrollWidth - metrics.innerWidth),
            broken,
            clipped,
            errors: [...new Set(errors)].slice(0, 5),
            unmocked: [...new Set(misses)],
            axe,
          };
          appendFileSync(join(OUT, 'report.jsonl'), JSON.stringify(row) + '\n');
          // Findings are collected into the report; they never fail the run.
        });
      }
    });
  }
}
