import type { Page } from '@playwright/test';
import * as d from './data';
import * as loop from '../../src/projects/loop/fixtures';

export interface MockResult {
  status: number;
  body: unknown;
}

const ok = (body: unknown): MockResult => ({ status: 200, body });
const notFound = (): MockResult => ({ status: 404, body: { detail: 'not found' } });

/**
 * Resolve an /api/** request to fixture JSON. Returns null when no fixture
 * exists; the caller records the miss and answers 501 so the page shows its
 * error state instead of hanging, and the scenario then fails on the miss.
 */
export function resolve(method: string, pathname: string): MockResult | null {
  // Ids such as 'acme/widgets' arrive percent-encoded as one path segment, so
  // match on the raw path and decode captured segments individually.
  const path = pathname;
  const get = method === 'GET';

  if (path === '/health' || path === '/api/health') return ok({ status: 'ok' });
  // Linking an existing Jira issue answers with the same body as GET .../jira.
  const jiraLinkPost = !get && path.match(/^\/api\/v1\/projects\/[^/]+\/tickets\/([^/]+)\/jira\/link$/);
  if (jiraLinkPost) return ok({ ...d.linkedTicketJiraLink, ticket_id: decodeURIComponent(jiraLinkPost[1]) });
  // Taking the Jira issue's text into the ticket: the ticket, the link and what changed.
  if (!get && /^\/api\/v1\/projects\/[^/]+\/tickets\/[^/]+\/jira\/pull$/.test(path)) {
    return ok({ ticket: d.linkedTicket, jira: d.linkedTicketJiraLink, updated_fields: [] });
  }
  // The Jira publish dialog: a draft, then a publish Jira accepted with warnings.
  const jiraPreviewPost = !get && path.match(/^\/api\/v1\/projects\/[^/]+\/tickets\/([^/]+)\/jira\/preview$/);
  if (jiraPreviewPost) return ok(d.jiraPreview(decodeURIComponent(jiraPreviewPost[1])));
  const jiraPublishPost = !get && path.match(/^\/api\/v1\/projects\/[^/]+\/tickets\/([^/]+)\/jira\/publish$/);
  if (jiraPublishPost) return ok(d.jiraPublishWithWarnings(decodeURIComponent(jiraPublishPost[1])));
  if (!get && /^\/api\/v1\/orgs\/[^/]+\/jira\/fields\/detect$/.test(path)) return ok(d.jiraDetectedFields);
  // Meetings (GitHub #116): the promote draft, a promote, an extraction.
  if (!get && /^\/api\/v1\/meetings\/[^/]+\/action-items\/[^/]+\/promote\/preview$/.test(path)) {
    return ok(path.includes(d.GROUP_MEETING_ID)
      ? { ...d.meetingPromotePreview, meeting_id: d.GROUP_MEETING_ID, target_project_id: null, target_project_options: [{ id: d.PROJECT_ID, name: 'Acme Widgets' }], jira_publish_available: false }
      : d.meetingPromotePreview);
  }
  if (!get && /^\/api\/v1\/meetings\/[^/]+\/action-items\/[^/]+\/promote$/.test(path)) {
    return ok({ ticket_id: 'tk_fixture7', item_id: 'mai_f1', status: 'promoted', published_to_jira: false, warnings: [] });
  }
  if (!get && /^\/api\/v1\/meetings\/[^/]+\/extract$/.test(path)) return ok({ ...d.meetingDetail, dropped_items: 0 });
  if (!get) return ok({ status: 'ok' });

  // Review Loop org-wide inbox and the public share page.
  if (path === '/api/v1/me/loops/needs-you') return ok(loop.needsYou());
  if (path === '/api/v1/me/loops/needs-you/count') return ok({ count: loop.needsYou().counts.total });
  if (path === '/api/v1/share/loop/story-token') return ok(loop.story());
  if (path === '/api/v1/share/loop/ship-token') return ok(loop.publicShipReport());
  if (/^\/api\/v1\/share\/loop\//.test(path)) return { status: 404, body: { detail: 'Share link not found' } };

  // ── account ──
  if (path === '/api/v1/auth/me') return ok(d.me);
  if (path === '/api/v1/billing/status') return ok(d.billingStatus);

  // ── sessions ──
  if (path === '/api/v1/sessions') return ok(d.sessionList);
  if (path === '/api/v1/sessions/models') return ok(d.sessionModels);
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
    if (rest === '') {
      // The detail carries the groups containing an org project (GitHub #101).
      return ok(project.org_id ? { ...project, groups: [d.projectGroups[0]] } : project);
    }
    if (rest === '/groups') return ok(project.org_id ? [d.projectGroups[0]] : []);
    if (rest === '/entries') return ok({ entries: d.knowledgeEntries, total: d.knowledgeEntries.length });
    if (rest === '/health') return ok(d.projectHealth);
    if (rest === '/compilations') return ok({ compilations: d.compilations });
    if (rest === '/pages') return ok({ pages: d.wikiPages });
    if (rest.startsWith('/pages/')) return ok({ ...d.wikiPages[0], backlinks: [] });
    if (rest === '/personas') return ok(d.personas);
    if (rest === '/assignable-users') return ok(d.assignableUsers);
    if (rest === '/tickets') return ok(d.tickets.map((t) => (t.id === d.LINKED_TICKET_ID ? d.linkedTicket : t)));
    // Review Loop (shapes follow routes/loops.py, review_loop.py, loop_checklist.py, spend.py).
    if (rest === '/loops') return ok(loop.loops());
    if (rest === '/loop-insights') return ok(loop.insights());
    if (rest === '/releases') return ok(loop.releaseList());
    if (rest.startsWith('/releases/') && rest.endsWith('/ship-report')) return ok(loop.shipReport());
    if (rest === '/ship-report') return ok(loop.shipReport());
    if (rest === '/loop-checklist') return ok(loop.checklist());
    if (rest === '/loop-share-links') return ok(loop.shareLinks());
    const loopTicket = rest.match(/^\/tickets\/(tk_child\d)\/(story|spend)$/);
    if (loopTicket) {
      if (loopTicket[2] === 'story') return ok(loop.story());
      return ok({ totals: { runs: 3, input_tokens: 180000, output_tokens: 60000, cache_read_tokens: 0, cache_write_tokens: 0, tokens_on_plans: 0, cost_usd: 1.4 }, per_persona: [], per_model: [], review_rounds: 3 });
    }
    const tk = rest.match(/^\/tickets\/([^/]+)(\/.*)?$/);
    if (tk) {
      const tid = decodeURIComponent(tk[1]);
      const sub = tk[2] ?? '';
      const linked = tid === d.LINKED_TICKET_ID;
      const base = d.tickets.find((t) => t.id === tid);
      if (!base) return notFound();
      if (sub === '') return ok(linked ? d.linkedTicket : base);
      if (sub === '/comments') return ok(linked ? d.linkedTicketComments : tid === d.tickets[1].id ? d.openTicketComments : []);
      if (sub === '/watchers') return ok(linked ? d.linkedTicketWatchers : []);
      if (sub === '/review-state') return ok(linked ? d.linkedTicketReviewState : { ticket_id: tid, review_state: null });
      if (sub === '/jira-sync-events') return ok(linked ? d.linkedTicketSyncEvents : { events: [] });
      if (sub === '/jira') return linked ? ok(d.linkedTicketJiraLink) : notFound();
      if (sub === '/events') return ok(linked ? d.linkedTicketEvents : { events: [], next_before: null });
      if (sub === '/jira/templates') return ok(d.jiraTicketTemplates);
      return null;
    }
    if (rest === '/meetings') return ok(d.projectMeetings);
    if (rest === '/agent-runs') return ok(d.agentRuns);
    if (rest === '/rules') return ok(d.rules.rules);
    if (rest === '/rules/versions') return ok(d.rulesVersions);
    const rulesVersion = rest.match(/^\/rules\/versions\/(\d+)$/);
    if (rulesVersion) return ok(d.rulesVersionDetail(Number(rulesVersion[1])));
    if (rest === '/compile-status') return ok(d.compileStatus);
    if (rest === '/repos') return ok(project.repos ?? []);
    if (rest === '/skill-install-requests') return ok({ items: [], can_approve: true });
    if (rest === '/settings') return ok({ auto_narrative: project.auto_narrative });
    if (rest === '/confluence/links') return ok(d.confluenceLinks);
    if (rest === '/jira/sprints') return ok(d.jiraSprints);
    return null;
  }

  // ── marketplace ──
  if (path === '/api/v1/marketplace/packages') return ok(d.marketplacePackages);
  if (path === '/api/v1/marketplace/revocations') return ok({ items: [], events: [], next_cursor: null, has_more: false });
  if (path === '/api/v1/marketplace/categories') return ok({ items: d.marketplaceCategories, categories: d.marketplaceCategories });
  if (path === '/api/v1/marketplace/catalogs') return ok(d.nativeCatalogs);
  if (path === '/api/v1/users/me/skills/packages') return ok({ items: [], count: 0 });
  if (path === '/api/v1/users/me/skills/assignments') return ok({ items: [], count: 0 });
  if (path === '/api/v1/publishers') return ok(d.myPublishers);
  if (/^\/api\/v1\/publishers\/[^/]+\/listings$/.test(path)) return ok(d.publisherListings);
  if (path === '/api/v1/marketplace/reports') return ok(d.marketplaceReports);
  if (/^\/api\/v1\/marketplace\/versions\/[^/]+\/works-in$/.test(path)) return ok(d.marketplaceWorksIn);
  if (/^\/api\/v1\/marketplace\/versions\/[^/]+\/disclosure$/.test(path)) return ok(d.marketplaceDisclosure);
  const mpkg = path.match(/^\/api\/v1\/marketplace\/packages\/([^/]+)(\/readme)?$/);
  if (mpkg) {
    const found = d.marketplacePackages.items.find((p) => p.package_id === decodeURIComponent(mpkg[1]));
    if (!found) return notFound();
    if (mpkg[2]) {
      return ok({
        version_id: found.version_id, package_id: found.package_id, source: 'readme', path: 'README.md',
        content: `# ${found.display_name}\n\nReviews a change before it lands.\n\n## Usage\n\nAsk your agent to review the current branch.`,
        hidden_unicode: [], truncated: false, untrusted: true, image_path_prefix: '',
      });
    }
    return ok(found);
  }

  // ── organization ──
  if (path === '/api/v1/org') return ok(d.orgInfo);
  if (path === '/api/v1/org/invites') return ok(d.orgInvites);
  if (path === '/api/v1/org/invites/me') return ok({ invites: [] });
  if (path === '/api/v1/orgs') return ok(d.myOrgs);
  const org = path.match(/^\/api\/v1\/orgs\/([^/]+)(\/.*)?$/);
  if (org) {
    const rest = org[2] ?? '';
    if (rest === '/settings') return ok(d.orgSettings);
    if (rest === '/groups') return ok(d.projectGroups);
    if (/^\/groups\/[^/]+$/.test(rest)) return ok(d.projectGroupDetail);
    if (rest === '/personas') return ok(d.orgPersonas);
    if (rest === '/sso/provider') return ok(d.ssoProvider);
    if (rest === '/sso/domains') return ok(d.ssoDomains);
    if (rest === '/sso/break-glass') return ok([]);
    if (rest === '/owner/transfer') return ok({ transfer: null });
    if (rest === '/members') {
      return ok({
        org_id: d.ORG_ID,
        members: d.orgInfo.members,
        seats_used: d.orgInfo.org.seats_used,
        seats_limit: d.orgInfo.org.seats_limit,
        current_user_role: d.orgInfo.current_user_role,
      });
    }
    if (rest === '/members/me/digest') return ok({ org_id: decodeURIComponent(org[1]), opt_out: false });
    if (rest === '/helper-settings') return ok(d.orgHelperSettingsAdmin);
    if (rest === '/llm-settings') return ok(d.orgLlmSettings);
    if (rest === '/skills/assignments/updates/count') return ok({ count: 0, has_more: false });
    if (rest.startsWith('/skills/')) return ok({ items: [], count: 0, has_more: false });
    if (rest === '/integrations/confluence') return ok(d.confluenceConnection);
    if (rest === '/integrations/confluence/mappings') return ok(d.confluenceMappings);
    if (rest === '/integrations/confluence/health') return ok(d.confluenceHealth);
    if (rest === '/integrations/confluence/spaces') {
      return ok({ spaces: [{ key: 'ENG', name: 'Engineering' }], has_more: false, query: null, limit: 50 });
    }
    if (/^\/integrations\/confluence\/spaces\/[^/]+\/pages$/.test(rest)) return ok([{ id: '65538', title: 'Engineering home' }]);
    if (rest === '/integrations/jira') return ok(d.jiraConnection);
    if (rest === '/integrations/jira/mappings') return ok(d.jiraMappings);
    if (rest === '/integrations/jira/health') return ok(d.jiraHealth);
    if (rest === '/integrations/jira/sync-events') return ok(d.jiraSyncEvents);
    // The built-in templates exactly as the server lists them, then the
    // fixture org's one custom template.
    if (rest === '/integrations/jira/templates') return ok([...d.jiraBuiltinTemplates, ...d.jiraCustomTemplates]);
    if (rest === '/integrations/confluence/templates') return ok(d.confluenceBuiltinTemplates);
    // Search terms arrive as ?q= — visual scenarios that type route that
    // themselves; the bare path is the unfiltered first page.
    if (rest === '/integrations/jira/projects') return ok(d.jiraProjectPage(''));
    if (/^\/integrations\/jira\/projects\/[^/]+\/issue-types$/.test(rest)) {
      return ok([{ name: 'Story', id: '10001' }, { name: 'Bug', id: '10004' }]);
    }
    return null;
  }
  // ── meetings ──
  if (/^\/api\/v1\/groups\/[^/]+\/meetings$/.test(path)) return ok(d.groupMeetings);
  const meeting = path.match(/^\/api\/v1\/meetings\/([^/]+)$/);
  if (meeting) {
    const mid = decodeURIComponent(meeting[1]);
    if (mid === d.MEETING_ID) return ok(d.meetingDetail);
    if (mid === d.GROUP_MEETING_ID) return ok(d.groupMeetingDetail);
    return { status: 404, body: { error: { code: '404', message: 'Meeting not found' } } };
  }
  // A promoted item in a group meeting looks its ticket up to link it.
  const ticketById = path.match(/^\/api\/v1\/tickets\/([^/]+)$/);
  if (ticketById) {
    const t = d.tickets.find((x) => x.id === decodeURIComponent(ticketById[1]));
    return t ? ok({ ...t, project_name: 'Acme Widgets', comment_count: 0, recent_comments: [] }) : notFound();
  }
  if (path === '/api/v1/transfers') return ok({ transfers: [] });
  if (path === '/api/v1/me/groups') return ok(d.myGroups);

  // ── notifications ──
  if (path === '/api/v1/me/ticket-notifications') return ok(d.ticketNotifications);
  if (path === '/api/v1/me/ticket-notifications/unread-count') return ok({ count: 3 });
  if (path === '/api/v1/me/notification-settings') return ok({ ticket_email: 'your_turn' });

  // ── settings ──
  if (path === '/api/v1/settings/audit-trigger') return ok({ trigger: 'manual', auto_audit: false, enabled: false });
  if (path === '/api/v1/settings/github') return ok(d.githubInstallation);
  if (path === '/api/v1/settings/gitlab') return ok(d.gitlabSettings);
  if (path === '/api/v1/settings/judge') return ok(d.judgeSettings);
  if (path === '/api/v1/llm/providers') return ok(d.llmProviders);
  if (path === '/api/v1/settings/helper') return ok(d.userHelperSettings);
  if (path === '/api/v1/helper/status') return ok(d.helperStatus);
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
    // A 501 rather than an empty 200: an empty object in place of a list
    // crashes some pages, which hides which request was missing.
    const r = result ?? { status: 501, body: { detail: `unmocked in the visual harness: ${url.pathname}` } };
    await route.fulfill({
      status: r.status,
      contentType: 'application/json',
      headers: { etag: '"fixture"' },
      body: JSON.stringify(r.body),
    });
  });
  return misses;
}
