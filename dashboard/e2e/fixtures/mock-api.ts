import type { Page } from '@playwright/test';
import * as d from './data';

export interface MockResult {
  status: number;
  body: unknown;
}

const ok = (body: unknown): MockResult => ({ status: 200, body });
const notFound = (): MockResult => ({ status: 404, body: { detail: 'not found' } });

/**
 * Resolve an /api/** request to fixture JSON. Returns null when no fixture
 * exists; the caller records the miss and answers with an empty object so a
 * page never hangs on an unmocked request.
 */
export function resolve(method: string, pathname: string): MockResult | null {
  // Ids such as 'acme/widgets' arrive percent-encoded as one path segment, so
  // match on the raw path and decode captured segments individually.
  const path = pathname;
  const get = method === 'GET';

  if (path === '/health' || path === '/api/health') return ok({ status: 'ok' });
  if (!get) return ok({ status: 'ok' });

  // ── account ──
  if (path === '/api/v1/auth/me') return ok(d.me);
  if (path === '/api/v1/billing/status') return ok(d.billingStatus);

  // ── sessions ──
  if (path === '/api/v1/sessions') return ok(d.sessionList);
  if (path === '/api/v1/sessions/search') return ok({ results: [], total: 0, page: 1, page_size: 20, has_more: false });
  if (path === '/api/v1/bookmarks/folders') return ok({ folders: [] });
  if (path === '/api/v1/bookmarks') return ok({ bookmarks: [] });

  // ── handoffs ──
  if (path === '/api/v1/handoffs/inbox') return ok(d.handoffInbox);
  if (path === '/api/v1/handoffs/sent') return ok(d.handoffSent);

  // ── projects ──
  if (path === '/api/v1/projects' || path === '/api/v1/projects/') return ok(d.projects);
  const proj = path.match(/^\/api\/v1\/projects\/([^/]+)(\/.*)?$/);
  if (proj) {
    const id = decodeURIComponent(proj[1]);
    const rest = proj[2] ?? '';
    const project = d.projects.find((p) => p.id === id || p.git_remote_normalized === id);
    if (!project) return notFound();
    if (rest === '') return ok(project);
    if (rest === '/entries') return ok({ entries: d.knowledgeEntries, total: d.knowledgeEntries.length });
    if (rest === '/health') return ok(d.projectHealth);
    if (rest === '/compilations') return ok({ compilations: d.compilations });
    if (rest === '/pages') return ok({ pages: d.wikiPages });
    if (rest.startsWith('/pages/')) return ok({ ...d.wikiPages[0], backlinks: [] });
    if (rest === '/personas') return ok(d.personas);
    if (rest === '/tickets') return ok(d.tickets);
    if (/^\/tickets\/[^/]+\/comments$/.test(rest)) return ok({ comments: [] });
    if (rest === '/agent-runs') return ok(d.agentRuns);
    if (rest === '/rules') return ok(d.rules.rules);
    if (rest === '/rules/versions') return ok(d.rulesVersions);
    if (rest === '/repos') return ok(project.repos ?? []);
    if (rest === '/skill-install-requests') return ok({ items: [], can_approve: true });
    if (rest === '/settings') return ok({ auto_narrative: project.auto_narrative });
    return null;
  }

  // ── marketplace ──
  if (path === '/api/v1/marketplace/packages') return ok(d.marketplacePackages);
  if (path === '/api/v1/marketplace/revocations') return ok({ items: [], events: [], next_cursor: null, has_more: false });
  if (path === '/api/v1/marketplace/categories') return ok({ items: d.marketplaceCategories, categories: d.marketplaceCategories });
  if (path === '/api/v1/marketplace/catalogs') return ok(d.nativeCatalogs);
  if (path === '/api/v1/users/me/skills/packages') return ok({ items: [], count: 0 });
  if (path === '/api/v1/users/me/skills/assignments') return ok({ items: [], count: 0 });
  if (path === '/api/v1/publishers') return ok({ items: [], count: 0 });

  // ── organization ──
  if (path === '/api/v1/org') return ok(d.orgInfo);
  if (path === '/api/v1/org/invites') return ok(d.orgInvites);
  if (path === '/api/v1/org/invites/me') return ok({ invites: [] });
  if (path === '/api/v1/orgs') return ok([{ org_id: d.ORG_ID, name: 'Acme Fixtures', role: 'admin' }]);
  const org = path.match(/^\/api\/v1\/orgs\/([^/]+)(\/.*)?$/);
  if (org) {
    const rest = org[2] ?? '';
    if (rest === '/settings') return ok(d.orgSettings);
    if (rest === '/personas') return ok(d.orgPersonas);
    if (rest === '/sso/provider') return ok(d.ssoProvider);
    if (rest === '/sso/domains') return ok(d.ssoDomains);
    if (rest === '/sso/break-glass') return ok([]);
    if (rest === '/owner/transfer') return ok({ transfer: null });
    if (rest === '/members') return ok(d.orgInfo.members);
    if (rest.startsWith('/skills/')) return ok({ items: [], count: 0, has_more: false });
    return null;
  }
  if (path === '/api/v1/transfers') return ok({ transfers: [] });

  // ── settings ──
  if (path === '/api/v1/settings/audit-trigger') return ok({ trigger: 'manual', auto_audit: false, enabled: false });
  if (path === '/api/v1/settings/github') return ok(d.githubInstallation);
  if (path === '/api/v1/settings/gitlab') return ok(d.gitlabSettings);
  if (path === '/api/v1/settings/judge') return ok(d.judgeSettings);
  if (path === '/api/v1/sync/settings') return ok(d.syncSettings);
  if (path === '/api/v1/sync/status') return ok(d.syncStatus);
  if (path === '/api/v1/sync/watchlist') return ok({ sessions: [] });
  if (path === '/api/v1/sync/health') return ok({ status: 'ok' });
  if (path === '/api/v1/dlp/policy') return ok(d.dlpPolicy);

  return null;
}

/** Install the interceptor; returns the list that collects unmocked requests. */
export async function mockApi(page: Page): Promise<string[]> {
  const misses: string[] = [];
  await page.route((u) => u.pathname.startsWith('/api/') || u.pathname === '/health', async (route) => {
    const url = new URL(route.request().url());
    const result = resolve(route.request().method(), url.pathname);
    if (!result) misses.push(`${route.request().method()} ${url.pathname}`);
    const r = result ?? { status: 200, body: {} };
    await route.fulfill({
      status: r.status,
      contentType: 'application/json',
      headers: { etag: '"fixture"' },
      body: JSON.stringify(r.body),
    });
  });
  return misses;
}
