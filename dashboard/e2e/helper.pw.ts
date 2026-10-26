import { test, type Page } from '@playwright/test';
import { appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { mockApi } from './fixtures/mock-api';
import { PROJECT_ID, me, projects, sessions } from './fixtures/data';

/**
 * Visual scenarios for the session-helper drawer. The helper API is mocked on
 * top of the shared fixtures: status, conversation list/get, and the SSE turn
 * stream (a complete stream via route.fulfill; a mid-answer stream by patching
 * fetch so the body stays open). Screenshots are viewport-sized because the
 * drawer is fixed over the page.
 */

const require = createRequire(import.meta.url);
const AXE_PATH = require.resolve('axe-core/axe.min.js');

const OUT = process.env.SFS_VISUAL_OUT!;
const PORT = process.env.SFS_VISUAL_PORT!;
const BASE_URL = `http://127.0.0.1:${PORT}`;
const FAKE_KEY = 'sk_sfs_FAKE_VISUAL_TEST_KEY_0000000000';

const NOW = Date.parse('2026-10-25T12:00:00Z');
const ago = (mins: number) => new Date(NOW - mins * 60_000).toISOString();

const conversations = [
  { id: 'hc_1', org_id: 'org_fixture01', title: 'Why did the billing webhook retry twice?', created_at: ago(30), updated_at: ago(12) },
  { id: 'hc_2', org_id: 'org_fixture01', title: 'Summarize last week on the sync queue', created_at: ago(60 * 26), updated_at: ago(60 * 26) },
  { id: 'hc_3', org_id: null, title: 'Where did we decide on Redis vs Postgres advisory locks for the importer?', created_at: ago(60 * 24 * 4), updated_at: ago(60 * 24 * 4) },
  { id: 'hc_4', org_id: null, title: null, created_at: ago(60 * 24 * 12), updated_at: ago(60 * 24 * 12) },
];

const S1 = sessions[0];
const S2 = sessions[1];

const ANSWER = [
  'The duplicate retry came from the **webhook handler acknowledging late** — Stripe re-sent the event before the first attempt committed [session:' + S1.id + ' msg:42].',
  '',
  'What the team changed:',
  '',
  '1. Persist the event id in `stripe_events` *before* doing work, and skip ids already seen [session:' + S1.id + ' msg:57].',
  '2. Move the email side-effect behind the commit so a retry can no longer send twice.',
  '3. Added a regression test that replays the same event concurrently [session:' + S2.id + '].',
  '',
  '```python',
  'if await seen(event.id):',
  '    return Response(status_code=200)',
  'await record(event.id)',
  '```',
  '',
  '| Attempt | Result |',
  '|---|---|',
  '| 1 | committed after 31 s |',
  '| 2 | duplicate email sent |',
].join('\n');

function sse(frames: Array<[string, unknown]>): string {
  return frames.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join('');
}

const TOOL_FRAMES: Array<[string, unknown]> = [
  ['tool_call', { id: 'tc_1', name: 'search_sessions', args: { query: 'billing webhook retry' } }],
  ['tool_result', { id: 'tc_1', name: 'search_sessions', ok: true, item_count: 6, session_ids: [S1.id, S2.id], message_indexes: [] }],
  ['tool_call', { id: 'tc_2', name: 'get_session_excerpts', args: { session_id: S1.id, query: 'retry' } }],
  ['tool_result', { id: 'tc_2', name: 'get_session_excerpts', ok: true, item_count: 1, session_ids: [S1.id], message_indexes: [42, 57] }],
];

const ANSWER_FRAMES: Array<[string, unknown]> = [
  ['conversation', { conversation_id: 'hc_new' }],
  ...TOOL_FRAMES,
  ...ANSWER.match(/[\s\S]{1,40}/g)!.map((delta) => ['text', { delta }] as [string, unknown]),
  ['citation', { session_id: S1.id, message_index: 42, title: S1.title }],
  ['citation', { session_id: S1.id, message_index: 57, title: S1.title }],
  ['citation', { session_id: S2.id, message_index: null, title: S2.title }],
  ['done', { stop_reason: 'stop', input_tokens: 1200, output_tokens: 260, key_source: 'user' }],
];

// Ids the helper mentions as plain text / inline code; the drawer links them.
const TICKET_ID = 'tk_008676e806e94bc5';
const LINK_ANSWER = [
  `The retry fix is tracked in ${TICKET_ID} (still in progress) and was reviewed in \`${S1.id}\`.`,
  '',
  `- Merged in PR #394, commit 839c3a7 [session:${S1.id} msg:57]`,
  `- Follow-up ticket \`tk_5c124c496ea14ecc\` covers the email side-effect`,
  `- Project: \`${PROJECT_ID}\``,
  '',
  '```bash',
  `sfs ticket show ${TICKET_ID}`,
  '```',
].join('\n');

function framesFor(answer: string): Array<[string, unknown]> {
  return [
    ['conversation', { conversation_id: 'hc_new' }],
    ...TOOL_FRAMES,
    ...answer.match(/[\s\S]{1,40}/g)!.map((delta) => ['text', { delta }] as [string, unknown]),
    ['citation', { session_id: S1.id, message_index: 57, title: S1.title }],
    ['done', { stop_reason: 'stop', input_tokens: 900, output_tokens: 140, key_source: 'user' }],
  ];
}

const TICKET_PREVIEW = {
  id: TICKET_ID,
  project_id: PROJECT_ID,
  project_name: projects[0].name,
  title: 'Retry Stripe webhooks idempotently and stop the duplicate receipt email',
  description:
    'Stripe re-sends `invoice.paid` when the handler acknowledges after **30 s**. Record the event id before doing work and move the email behind the commit.\n\n- Persist `stripe_events.id` first\n- Skip ids already seen\n- Replay test with two concurrent deliveries',
  priority: 'high',
  assigned_to: 'atlas',
  created_by_user_id: me.user_id,
  created_by_session_id: null,
  created_by_persona: 'compass',
  status: 'in_progress',
  kind: 'task',
  parent_ticket_id: null,
  child_ticket_ids: [],
  context_refs: [],
  file_refs: [],
  related_sessions: [],
  acceptance_criteria: ['No duplicate email on a replayed event', 'Replay test passes 50 runs', 'Docs updated'],
  resolver_session_id: null,
  resolver_user_id: null,
  completion_notes: null,
  changed_files: [],
  knowledge_entry_ids: [],
  depends_on: [],
  created_at: ago(60 * 24 * 3),
  updated_at: ago(20),
  resolved_at: null,
  comment_count: 7,
  recent_comments: [
    { id: 'tc_7', ticket_id: TICKET_ID, author_user_id: me.user_id, author_persona: 'codex-reviewer', content: 'R2: VERIFIED-CLEAN. The replay test now covers the concurrent path and the email moved behind the commit.', session_id: null, created_at: ago(18) },
    { id: 'tc_6', ticket_id: TICKET_ID, author_user_id: me.user_id, author_persona: 'atlas', content: 'Pushed 839c3a7: records the event id inside the same transaction as the invoice update.', session_id: null, created_at: ago(95) },
    { id: 'tc_5', ticket_id: TICKET_ID, author_user_id: me.user_id, author_persona: null, content: 'Can we also make sure the receipt email uses the idempotency key so the provider dedupes on its side?', session_id: null, created_at: ago(60 * 26) },
  ],
};

// Two confirm-first write actions proposed in one answer: a pending trash
// cleanup and an Auto Narrative change the scenario then confirms.
const TRASH_ACTION_ID = 'ha_00000000000000000000aaaa';
const NARRATIVE_ACTION_ID = 'ha_00000000000000000000bbbb';
const inMins = (mins: number) => new Date(NOW + mins * 60_000).toISOString();
const TRASH_TARGETS = [
  ...sessions.slice(0, 6).map((s) => ({
    id: s.id,
    name: s.title,
    change: 'will_change',
    before: 'active',
    after: 'trashed',
    tool: s.source_tool,
    created_at: s.created_at,
  })),
  { id: 'ses_fixtureXX', name: 'Spike: try Redis for the import queue', change: 'skipped', reason: 'already_trashed' },
];
const NARRATIVE_TARGETS = [
  { id: PROJECT_ID, name: 'Acme Widgets', change: 'will_change', before: false, after: true },
  { id: 'proj_fixture02', name: 'Internal tools and a long project name that has to truncate', change: 'will_change', before: false, after: true },
  { id: 'proj_fixture03', name: 'Docs site', change: 'unchanged', before: true, after: true },
  { id: 'proj_fixture04', name: 'Billing service', change: 'skipped', reason: 'upgrade_required' },
];
const ACTION_FRAMES: Array<[string, unknown]> = [
  ['conversation', { conversation_id: 'hc_new' }],
  ['tool_call', { id: 'tc_a', name: 'trash_sessions', args: { session_ids: TRASH_TARGETS.map((t) => t.id) } }],
  ['tool_result', { id: 'tc_a', name: 'trash_sessions', ok: true, item_count: TRASH_TARGETS.length }],
  ['action_proposed', { action: { id: TRASH_ACTION_ID, kind: 'trash_sessions', summary: 'Move 6 session(s) to Trash', targets: TRASH_TARGETS, expires_at: inMins(14) } }],
  ['tool_call', { id: 'tc_b', name: 'set_project_auto_narrative', args: { enabled: true } }],
  ['tool_result', { id: 'tc_b', name: 'set_project_auto_narrative', ok: true, item_count: NARRATIVE_TARGETS.length }],
  ['action_proposed', { action: { id: NARRATIVE_ACTION_ID, kind: 'set_auto_narrative', summary: 'Set auto-narrative on for 2 project(s)', targets: NARRATIVE_TARGETS, expires_at: inMins(14) } }],
  ['text', { delta: 'I prepared both changes. Nothing happens until you confirm each card below.' }],
  ['done', { stop_reason: 'stop', input_tokens: 800, output_tokens: 60, key_source: 'user' }],
];

const STREAM_FRAMES: Array<[string, unknown]> = [
  ['conversation', { conversation_id: 'hc_new' }],
  ...TOOL_FRAMES.slice(0, 3),
];

type Scenario = {
  name: string;
  status?: { enabled: boolean; key_source: string | null; provider: string | null; model: string | null };
  meRole?: string;
  turn?: 'answer' | 'busy' | 'stream';
  ask?: string;
  /** Answer with proposed write actions; the Auto Narrative one is confirmed. */
  actions?: boolean;
  openHistory?: boolean;
  openScope?: boolean;
  scrollUp?: boolean;
  /** Answer with ticket / project / session / PR references. */
  links?: boolean;
  /** Open the preview of the first ticket reference. */
  preview?: boolean;
  /** Answer the first question, then refuse the follow-up with 429 helper_budget_exceeded. */
  budget?: boolean;
};

const OK_STATUS = { enabled: true, key_source: 'user', provider: 'anthropic', model: 'claude-sonnet-4' };

const SCENARIOS: Scenario[] = [
  { name: 'helper-empty' },
  { name: 'helper-answer', turn: 'answer', ask: 'Why did the billing webhook retry twice?' },
  { name: 'helper-action', turn: 'answer', ask: 'Clean up my old sessions and turn on narratives', actions: true },
  { name: 'helper-streaming', turn: 'stream', ask: 'Why did the billing webhook retry twice?' },
  { name: 'helper-history', openHistory: true },
  { name: 'helper-no-key', status: { enabled: true, key_source: null, provider: null, model: null }, meRole: 'member' },
  { name: 'helper-not-entitled', status: { enabled: false, key_source: null, provider: null, model: null } },
  { name: 'helper-busy', turn: 'busy', ask: 'Summarize this project' },
  { name: 'helper-scrolled-up', turn: 'answer', ask: 'Why did the billing webhook retry twice?', scrollUp: true },
  { name: 'helper-scope-menu', turn: 'answer', ask: 'Why did the billing webhook retry twice?', openScope: true },
  { name: 'helper-links', turn: 'answer', ask: 'Where is the webhook retry fix tracked?', links: true },
  { name: 'helper-usage-budget', turn: 'answer', ask: 'Why did the billing webhook retry twice?', budget: true, meRole: 'member', status: { ...OK_STATUS, key_source: 'org' } },
  { name: 'helper-ticket-preview', turn: 'answer', ask: 'Where is the webhook retry fix tracked?', links: true, preview: true },
];

async function installHelperMocks(page: Page, sc: Scenario) {
  const status = sc.status ?? OK_STATUS;
  let turnsSeen = 0;
  await page.route(
    (u) => u.pathname.startsWith('/api/v1/helper/') || u.pathname === '/api/v1/auth/me',
    async (route) => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      const json = (status: number, body: unknown) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (p === '/api/v1/auth/me') return json(200, { ...me, org_role: sc.meRole ?? me.org_role });
      if (p === '/api/v1/helper/status') return json(200, status);
      if (p === '/api/v1/helper/conversations') return json(200, conversations);
      if (p.startsWith('/api/v1/helper/conversations/')) {
        return json(200, {
          ...conversations[0],
          scope: { project_id: PROJECT_ID, session_ids: [] },
          messages: [
            { id: 'm1', role: 'user', content: conversations[0].title, citations: null },
            { id: 'm2', role: 'assistant', content: ANSWER, citations: [{ session_id: S1.id, message_index: 42, title: S1.title }] },
          ],
        });
      }
      if (sc.actions && p === '/api/v1/helper/turns') {
        return route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' },
          body: sse(ACTION_FRAMES),
        });
      }
      if (p === `/api/v1/helper/actions/${NARRATIVE_ACTION_ID}/confirm`) {
        return json(200, {
          id: NARRATIVE_ACTION_ID,
          kind: 'set_auto_narrative',
          status: 'confirmed',
          summary: 'Set auto-narrative on for 2 project(s)',
          targets: NARRATIVE_TARGETS,
          params: { project_ids: [PROJECT_ID, 'proj_fixture02'], enabled: true },
          result: { targets: [{ id: PROJECT_ID, ok: true, error: null }, { id: 'proj_fixture02', ok: false, error: 'not_admin' }] },
          created_at: ago(1),
          expires_at: inMins(14),
          decided_at: ago(0),
        });
      }
      if (p === '/api/v1/helper/turns') {
        turnsSeen += 1;
        if (sc.budget && turnsSeen > 1) {
          return json(429, {
            error: {
              code: 'helper_budget_exceeded',
              message: "This org's monthly session-helper token budget has been reached.",
              details: { error: 'helper_budget_exceeded' },
            },
          });
        }
        if (sc.turn === 'busy') {
          return json(409, {
            detail: {
              code: 'helper_busy',
              error: 'helper_busy',
              message: 'You already have the maximum number of helper turns running.',
            },
          });
        }
        return route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' },
          body: sse(sc.links ? framesFor(LINK_ANSWER) : ANSWER_FRAMES),
        });
      }
      return json(404, { detail: 'not found' });
    },
  );

  if (sc.links) {
    // One GitHub-verified repo, so PR and commit references link out too.
    await page.route(
      (u) => u.pathname === `/api/v1/projects/${PROJECT_ID}` || u.pathname.startsWith('/api/v1/tickets/'),
      (route) => {
        const p = new URL(route.request().url()).pathname;
        const body =
          p === `/api/v1/projects/${PROJECT_ID}`
            ? { ...projects[0], repos: projects[0].repos!.slice(0, 1) }
            : p === `/api/v1/tickets/${TICKET_ID}`
              ? TICKET_PREVIEW
              : null;
        return route.fulfill({
          status: body ? 200 : 404,
          contentType: 'application/json',
          body: JSON.stringify(body ?? { detail: 'Ticket not found' }),
        });
      },
    );
  }

  if (sc.turn === 'stream') {
    // Keep the body open after the queued frames so the drawer stays mid-answer.
    await page.addInitScript(
      ({ queued, partial }) => {
        const orig = window.fetch.bind(window);
        window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
          if (!url.includes('/api/v1/helper/turns')) return orig(input, init);
          const enc = new TextEncoder();
          const body = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(enc.encode(queued));
              setTimeout(() => controller.enqueue(enc.encode(partial)), 150);
            },
          });
          return Promise.resolve(new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }));
        };
      },
      {
        queued: sse(STREAM_FRAMES),
        partial: sse([['text', { delta: 'The duplicate retry came from the **webhook handler acknowledging late** — Stripe re-sent the event before' }]]),
      },
    );
  }
}

async function openDrawer(page: Page) {
  const btn = page.getByRole('button', { name: 'Ask the helper' }).last();
  await btn.click();
  await page.locator('[aria-labelledby="helper-panel-title"]').waitFor();
  await page.waitForTimeout(350);
}

async function ask(page: Page, text: string) {
  const box = page.getByLabel('Ask the session helper');
  await box.fill(text);
  await box.press('Enter');
}

const THEMES = ['dark', 'light'] as const;
const WIDTHS: Array<[number, number]> = [
  [1440, 900],
  [834, 1112],
  [390, 844],
];

for (const theme of THEMES) {
  for (const [width, height] of WIDTHS) {
    test.describe(`helper ${theme} ${width}`, () => {
      test.use({ viewport: { width, height }, colorScheme: theme });

      for (const sc of SCENARIOS) {
        test(sc.name, async ({ page }) => {
          await mockApi(page);
          await installHelperMocks(page, sc);
          const errors: string[] = [];
          page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));

          await page.addInitScript(
            ({ key, base, theme, now }) => {
              try {
                sessionStorage.setItem('sessionfs_auth', JSON.stringify({ apiKey: key, baseUrl: base }));
                localStorage.setItem('sfs-theme', theme);
              } catch {
                /* storage unavailable */
              }
              // Freeze "now" so relative times in the history list are stable.
              const RealDate = Date;
              const fixed = now;
              class FrozenDate extends RealDate {
                constructor(...args: unknown[]) {
                  if (args.length === 0) super(fixed);
                  else super(...(args as [string]));
                }
                static now() {
                  return fixed;
                }
              }
              (window as unknown as { Date: DateConstructor }).Date = FrozenDate as unknown as DateConstructor;
            },
            { key: FAKE_KEY, base: BASE_URL, theme, now: NOW },
          );

          await page.goto(`/projects/${PROJECT_ID}`, { waitUntil: 'networkidle' });
          await page.waitForTimeout(300);
          await openDrawer(page);

          if (sc.openHistory) {
            const historyBtn = page.getByRole('button', { name: /history|conversations/i }).first();
            if (await historyBtn.count()) await historyBtn.click();
            await page.waitForTimeout(250);
          }
          if (sc.ask) {
            await ask(page, sc.ask);
            await page.waitForTimeout(sc.turn === 'stream' ? 700 : 900);
          }
          if (sc.budget) {
            await ask(page, 'And the receipt email?');
            await page.getByText('Monthly budget reached').waitFor();
            await page.waitForTimeout(250);
          }
          if (sc.preview) {
            await page.getByRole('button', { name: `Preview ticket ${TICKET_ID}` }).first().click();
            await page.getByRole('heading', { name: TICKET_PREVIEW.title }).waitFor();
            await page.waitForTimeout(250);
          }
          if (sc.actions) {
            await page.getByRole('button', { name: 'Confirm', exact: true }).click();
            await page.getByText('Partly applied').waitFor();
            // Show the pending trash card at the top of the transcript.
            await page.getByRole('heading', { name: 'Move 6 sessions to Trash' }).evaluate((el) => {
              const card = el.closest('section')!;
              const scroller = document.querySelector('[aria-label="Conversation"]')!.parentElement!;
              scroller.scrollTop += card.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 16;
              scroller.dispatchEvent(new Event('scroll'));
            });
            await page.waitForTimeout(250);
          }
          if (sc.scrollUp) {
            await page.locator('[aria-label="Conversation"]').evaluate((el) => {
              const scroller = el.parentElement!;
              scroller.scrollTop = 0;
              scroller.dispatchEvent(new Event('scroll'));
            });
            await page.waitForTimeout(250);
          }
          if (sc.openScope) {
            const scopeBtn = page.getByRole('button', { name: /change scope/i });
            if (await scopeBtn.count()) await scopeBtn.click();
            await page.waitForTimeout(200);
          }

          const file = `${sc.name}__${theme}__${width}.png`;
          await page.screenshot({ path: join(OUT, file) });

          const clipped = await page.evaluate(() => {
            const dlg = document.querySelector('[aria-labelledby="helper-panel-title"]');
            if (!dlg) return ['no dialog'];
            const box = dlg.getBoundingClientRect();
            const out: string[] = [];
            for (const el of Array.from(dlg.querySelectorAll('*'))) {
              const r = el.getBoundingClientRect();
              if (r.width === 0 || r.height === 0) continue;
              if (el.closest('.sr-only')) continue;
              let scroller = false;
              for (let p = el.parentElement; p && p !== dlg; p = p.parentElement) {
                const o = getComputedStyle(p).overflowX;
                if ((o === 'auto' || o === 'scroll' || o === 'hidden') && p.scrollWidth > p.clientWidth) scroller = true;
              }
              if (scroller) continue;
              if (r.right > box.right + 1 || r.left < box.left - 1) {
                out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(/\s+/).slice(0, 3).join('.')} [${Math.round(r.left)}..${Math.round(r.right)}]`);
              }
            }
            return out.slice(0, 8);
          });

          await page.addScriptTag({ path: AXE_PATH });
          const axe = await page.evaluate(async () => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const res = await (window as any).axe.run('[aria-labelledby="helper-panel-title"]', { resultTypes: ['violations'] });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            return res.violations.map((v: any) => ({
              id: v.id,
              impact: v.impact,
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              detail: v.nodes.slice(0, 6).map((n: any) => `${String(n.target.join(' ')).slice(0, 80)} :: ${(n.any[0]?.message || n.failureSummary || '').slice(0, 140)}`),
            }));
          });

          appendFileSync(
            join(OUT, 'helper-report.jsonl'),
            JSON.stringify({ scenario: sc.name, theme, width, file, clipped, errors, axe }) + '\n',
          );
        });
      }
    });
  }
}
