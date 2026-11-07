/**
 * For You fixtures: the shape GET /api/v1/me/for-you returns, used by the
 * component tests and the strict visual harness (e2e/fixtures/mock-api.ts).
 * Times are relative to "now" so the page reads the same on any day.
 */
import type { AttentionFeed, AttentionItem, CollabProject } from '../api/client';

export const FY_PROJECT = 'proj_fixture_01';

const iso = (minutesFromNow: number) => new Date(Date.now() + minutesFromNow * 60_000).toISOString();
const day = (days: number) => {
  const d = new Date(Date.now() + days * 86_400_000);
  return d.toISOString().slice(0, 10);
};

const rank = (decides: number, urgency = 'No deadline'): AttentionItem['rank'] => [
  { factor: 'band', text: 'On your radar: due now, blocking, or harmful', ...(decides === 0 ? { decides: true } : {}) },
  { factor: 'involvement', text: 'Assigned to you or a persona you represent', ...(decides === 1 ? { decides: true } : {}) },
  { factor: 'urgency', text: urgency, due_at: null, ...(decides === 2 ? { decides: true } : {}) },
  { factor: 'recency', text: 'Most recent activity', at: iso(-120), ...(decides === 3 ? { decides: true } : {}) },
];

function item(over: Partial<AttentionItem> & Pick<AttentionItem, 'item_key' | 'kind' | 'group' | 'title' | 'why'>): AttentionItem {
  return {
    band: 'your_turn',
    involvement: 'assignee',
    occurred_at: iso(-90),
    due_at: null,
    link: `/projects/${FY_PROJECT}?tab=work&sub=tickets`,
    project_id: FY_PROJECT,
    actions: ['snooze', 'hide'],
    source: { type: 'ticket', id: over.item_key.replace('fy_', 'tk_') },
    context: {},
    rank: rank(3),
    ...over,
  };
}

export const fyItems: AttentionItem[] = [
  item({
    item_key: 'fy_harm1', kind: 'loop_harmful_findings', group: 'radar', band: 'now',
    title: 'Share list shows the creator’s email to people without read access',
    why: '1 harmful open finding on feat/rl-0294 · Acme Widgets',
    source: { type: 'ticket', id: 'tk_5d2a48eed3ab4446' },
    actions: ['open', 'snooze', 'hide'],
    link: `/projects/${FY_PROJECT}?tab=work&sub=loop&story=tk_5d2a48eed3ab4446`,
    context: {
      source_label: 'Review loop', ref: 'tk_5d2a48eed3ab4446', project_name: 'Acme Widgets', harm: 'harmful',
      facts: [['Branch', 'feat/rl-0294'], ['Head commit', 'a71c09e'], ['Round', '4'], ['Harmful open', '1']],
      people: [{ name: 'You', role: 'assignee', agent: false, you: true }],
      related: [{ item_key: 'fy_ticket1', title: 'Rotated service keys keep the original member’s identity', ref: 'tk_e540d17057a84d14', group: 'waiting' }],
    },
    rank: rank(0, 'A harmful finding is open'),
  }),
  item({
    item_key: 'fy_appr1', kind: 'approval', group: 'radar', band: 'now', involvement: 'approver',
    title: 'Auth changes (1/2)',
    why: 'Waiting on your approval · blocks merge',
    occurred_at: iso(-40), due_at: iso(4 * 1440),
    source: { type: 'approval_request', id: 'apr_19f2c4' },
    link: '/approvals?request=apr_19f2c4',
    actions: ['approve', 'reject', 'open', 'snooze', 'hide'],
    context: {
      source_label: 'Approval', ref: 'apr_19f2c4', project_name: 'Billing service',
      progress: [{ label: 'Approvals', value: 1, max: 2, text: '1 of 2' }],
      facts: [['Policy', 'Auth changes v3'], ['Ticket', 'tk_86db8de1c3bf4bf0'], ['Commit', '9be41c0d2a17'], ['Target', 'Move the Stripe webhook verifier secret to Secret Manager'], ['Approvals', '1 of 2'], ['Effect', 'Blocks the merge'], ['Expires', iso(4 * 1440)]],
      approval: {
        request_id: 'apr_19f2c4', policy_name: 'Auth changes', target_kind: 'merge', target_label: 'commit 9be41c0',
        target_title: 'Move the Stripe webhook verifier secret to Secret Manager', approved_count: 1, required_count: 2, blocking: true,
      },
      people: [{ name: 'You', role: 'approver', agent: false, you: true }],
    },
    rank: rank(0, 'Blocking policy keeps the merge check pending'),
  }),
  item({
    item_key: 'fy_rem1', kind: 'reminder', group: 'radar', band: 'now', involvement: 'owner',
    title: 'Book a follow-up with Tolu',
    why: 'Reminder · overdue · Infra sync',
    occurred_at: iso(-12), due_at: iso(-12),
    source: { type: 'reminder', id: 'rem_7c41e0' },
    link: '/reminders?reminder=rem_7c41e0',
    actions: ['done', 'snooze', 'hide'],
    context: {
      source_label: 'Reminder', ref: 'rem_7c41e0', project_name: 'Acme Widgets',
      facts: [['Due', iso(-12)], ['Time zone', 'Europe/London'], ['Source', 'meeting Infra sync']],
      people: [{ name: 'You', role: 'own it', agent: false, you: true }],
      reminder: { id: 'rem_7c41e0', timezone: 'Europe/London' },
    },
    rank: rank(2, 'Overdue'),
  }),
  item({
    item_key: 'fy_ticket1', kind: 'ticket', group: 'waiting',
    title: 'Rotated service keys keep the original member’s identity',
    why: 'Assigned to you · due today · priority high',
    due_at: iso(60), source: { type: 'ticket', id: 'tk_e540d17057a84d14' },
    actions: ['context', 'snooze', 'hide'],
    context: {
      source_label: 'Ticket', ref: 'tk_e540d17057a84d14', project_name: 'Acme Widgets',
      quote: { by: 'Description', text: 'A key rotated by a different member keeps every minter on the chain. A person reviewing work done with that key is not independent.' },
      facts: [['Status', 'open'], ['Priority', 'high'], ['Due', day(0)], ['Created', iso(-3 * 1440)]],
      people: [{ name: 'You', role: 'assignee', agent: false, you: true }, { name: 'Atlas', role: 'persona, created it', agent: true, you: false }],
      progress: [{ label: 'Acceptance criteria', value: null, max: 3, text: '3 criteria' }],
      ticket: { id: 'tk_e540d17057a84d14', project_id: FY_PROJECT, status: 'open' },
    },
    rank: rank(1),
  }),
  item({
    item_key: 'fy_loop1', kind: 'loop_needs_human', group: 'waiting', involvement: 'editor',
    title: 'Paused at its round cap: Dunning emails use the org’s locale',
    why: 'round cap hit (6 of 6) · Billing service',
    source: { type: 'ticket', id: 'tk_4d4e47ca4892414d#round cap hit (6 of 6)' },
    actions: ['raise', 'open', 'snooze', 'hide'],
    context: {
      loop: { id: 'tk_loop_fixture_01', project_id: FY_PROJECT, ticket_id: 'tk_4d4e47ca4892414d', limit: 'rounds' },
      source_label: 'Review loop', ref: 'tk_4d4e47ca4892414d', project_name: 'Billing service',
      facts: [['Reason', 'round cap reached'], ['Project', 'Billing service']],
      people: [{ name: 'You', role: 'can act', agent: false, you: true }],
    },
  }),
  item({
    item_key: 'fy_handoff1', kind: 'handoff', group: 'waiting', project_id: null,
    title: 'Dara handed you merge-check fallback',
    why: 'Handed to you · expires ' + day(5),
    source: { type: 'handoff', id: 'hnd_3f8a21' }, link: '/handoffs/hnd_3f8a21', due_at: iso(5 * 1440),
    actions: ['claim', 'open', 'snooze', 'hide'],
    context: {
      source_label: 'Handoff', ref: 'hnd_3f8a21',
      quote: { by: 'Dara Okon · handoff note', text: 'Installs without checks:write fall back to the PR comment path. I got as far as the retry; the session has the repro.' },
      facts: [['Tool', 'claude-code'], ['Messages', '82'], ['Expires', iso(5 * 1440)]],
      people: [{ name: 'Dara Okon', role: 'sent it', agent: false, you: false }],
    },
  }),
  item({
    item_key: 'fy_q1', kind: 'ticket_question', group: 'asking', involvement: 'assignee',
    title: 'Tolu Bello asked a question on “Cloud Armor per-path rule for delivery webhooks”',
    why: 'A question on your ticket · not answered',
    occurred_at: iso(-15 * 60), source: { type: 'ticket_comment', id: 'cmt_77' },
    actions: ['reply', 'answered', 'snooze', 'hide'],
    context: {
      source_label: 'Ticket comment', ref: 'tk_07fe22af490f4c4d', project_name: 'Acme Widgets',
      quote: { by: 'Tolu Bello · comment', text: '@ola rule 1470 covers github and gitlab. Should the per-org delivery path share the 120/min tier, or get its own?' },
      facts: [['Ticket', 'Cloud Armor per-path rule for delivery webhooks'], ['Asked', iso(-15 * 60)]],
      people: [{ name: 'Tolu Bello', role: 'asked', agent: false, you: false }],
      reply: { project_id: FY_PROJECT, ticket_id: 'tk_07fe22af490f4c4d', to: 'Tolu Bello' },
    },
  }),
  item({
    item_key: 'fy_mtg1', kind: 'meeting_action', group: 'meetings', involvement: 'owner',
    title: 'Write the 0.29.5 upgrade note for self-hosted installs',
    why: 'From meeting “Release sync” · you were named · due ' + day(3) + ' · not a ticket yet',
    due_at: iso(3 * 1440), source: { type: 'meeting_item', id: 'mai_f1' }, link: '/meetings/mtg_fixture_01?lines=18-20',
    actions: ['promote', 'remind', 'dismiss'],
    context: {
      source_label: 'Meeting', ref: 'mtg_fixture_01 · L18-20', project_name: 'Acme Widgets',
      quote: { by: 'Release sync · L18-20 · Kemi Ade', text: 'Kemi Ade: Ola, can you write the 0.29.5 upgrade note for self-hosted before Monday? Migration 118 needs a line about rotated keys.' },
      speaker: 'Kemi Ade',
      suggest: {
        level: 3, wording: 'Sounds like an action for you', owner: 'You', due: day(3),
        destination: { kind: 'project', id: FY_PROJECT, name: 'Acme Widgets' },
        title: 'Write the 0.29.5 upgrade note for self-hosted installs',
      },
      meeting: { id: 'mtg_fixture_01', item_id: 'mai_f1', title: 'Release sync', lines: '18-20', kind: 'meeting', project_id: FY_PROJECT, group_id: null, meeting_at: iso(-1440) },
      facts: [['Meeting', 'Release sync'], ['Cited lines', 'L18-20'], ['Said by', 'Kemi Ade'], ['Goes to', 'Acme Widgets (project)']],
      people: [{ name: 'Kemi Ade', role: 'said it', agent: false, you: false }, { name: 'You', role: 'suggested owner', agent: false, you: true }],
    },
  }),
  item({
    item_key: 'fy_mtg2', kind: 'meeting_suggestion', group: 'meetings', involvement: 'member',
    title: 'Backfill the locale for orgs created before migration 112',
    why: 'From meeting “Billing sync” · nobody was named · you took part · not a ticket yet',
    source: { type: 'meeting_item', id: 'mai_g1' }, link: '/meetings/mtg_fixture_02?lines=40-41', project_id: null,
    actions: ['promote', 'remind', 'dismiss'],
    context: {
      source_label: 'Meeting', ref: 'mtg_fixture_02 · L40-41', project_name: 'Release train',
      quote: { by: 'Billing sync · L40-41 · Dara Okon', text: 'Dara Okon: We probably need to backfill the locale for orgs created before migration 112. Not sure who has it.' },
      speaker: 'Dara Okon',
      suggest: {
        level: 1, wording: 'Might be an action for you', owner: 'Not named', due: null,
        destination: { kind: 'group', id: 'grp_fixture_01', name: 'Release train' },
        title: 'Backfill the locale for orgs created before migration 112',
      },
      meeting: { id: 'mtg_fixture_02', item_id: 'mai_g1', title: 'Billing sync', lines: '40-41', kind: 'meeting', project_id: null, group_id: 'grp_fixture_01', meeting_at: iso(-2 * 1440) },
      facts: [['Meeting', 'Billing sync'], ['Cited lines', 'L40-41'], ['Goes to', 'Release train (group)']],
      people: [{ name: 'Dara Okon', role: 'said it', agent: false, you: false }, { name: 'You', role: 'took part', agent: false, you: true }],
    },
  }),
  item({
    item_key: 'fy_chat1', kind: 'chat_action', group: 'meetings', involvement: 'owner',
    title: 'Confirm the 0.29.5 ship date in #release-train',
    why: 'From chat “release-train” · you were named · not a ticket yet',
    occurred_at: iso(-200), source: { type: 'chat_item', id: 'mai_c1' }, link: '/meetings/chat_fixture_01?lines=12-13',
    actions: ['promote', 'remind', 'dismiss'],
    context: {
      source_label: 'Chat', ref: 'chat_fixture_01 · M12-13', project_name: 'Acme Widgets',
      quote: { by: 'release-train · M12-13 · Kemi Ade', text: 'Kemi Ade: @ola are we still shipping 0.29.5 on Monday, or waiting on the auth-changes approval? Kemi Ade: can you confirm here either way' },
      speaker: 'Kemi Ade',
      suggest: {
        level: 2, wording: 'Sounds like an action for you', owner: 'You', due: null,
        destination: { kind: 'project', id: FY_PROJECT, name: 'Acme Widgets' },
        title: 'Confirm the 0.29.5 ship date in #release-train',
      },
      meeting: { id: 'chat_fixture_01', item_id: 'mai_c1', title: 'release-train', lines: '12-13', range: 'M12-13', kind: 'chat', channel: '#release-train', project_id: FY_PROJECT, group_id: null, meeting_at: iso(-300) },
      facts: [['Chat thread', 'release-train'], ['Channel', '#release-train'], ['Cited messages', 'M12-13'], ['Said by', 'Kemi Ade'], ['Goes to', 'Acme Widgets (project)']],
      people: [{ name: 'Kemi Ade', role: 'said it', agent: false, you: false }, { name: 'You', role: 'suggested owner', agent: false, you: true }],
    },
  }),
  item({
    item_key: 'fy_rem2', kind: 'reminder', group: 'coming', band: 'upcoming', involvement: 'owner',
    title: 'Renew the load balancer certificate for api.sessionfs.dev',
    why: 'Reminder · coming up',
    due_at: iso(3 * 1440), occurred_at: iso(3 * 1440), source: { type: 'reminder', id: 'rem_e19d02' }, link: '/reminders?reminder=rem_e19d02', project_id: null,
    actions: ['done', 'snooze', 'hide'],
    context: { source_label: 'Reminder', ref: 'rem_e19d02', facts: [['Due', iso(3 * 1440)]], people: [{ name: 'You', role: 'own it', agent: false, you: true }] },
  }),
  item({
    item_key: 'fy_quiet1', kind: 'ticket', group: 'second',
    title: 'Security low findings from the 0.29.1 review',
    why: 'Assigned to you · no activity for 8 days',
    occurred_at: iso(-8 * 1440), source: { type: 'ticket', id: 'tk_5c8ef73773fd43d6' },
    actions: ['context', 'snooze', 'hide'],
    context: {
      source_label: 'Ticket', ref: 'tk_5c8ef73773fd43d6', project_name: 'Acme Widgets', quiet_days: 8,
      quote: { by: 'Description', text: 'Three low findings from the 0.29.1 security review. None started.' },
      facts: [['Status', 'open'], ['Priority', 'low']],
      people: [{ name: 'You', role: 'assignee', agent: false, you: true }, { name: 'Sentinel', role: 'persona, created it', agent: true, you: false }],
      ticket: { id: 'tk_5c8ef73773fd43d6', project_id: FY_PROJECT, status: 'open' },
    },
  }),
  item({
    item_key: 'fy_kb1', kind: 'knowledge_changes', group: 'lately', band: 'lately', involvement: 'member',
    title: '2 decisions and 1 other change in Acme Widgets',
    why: 'Knowledge changed in a project you work in · last 7 days',
    source: { type: 'project', id: FY_PROJECT }, link: `/projects/${FY_PROJECT}?tab=knowledge&sub=history`,
    actions: ['read', 'open', 'hide'],
    context: {
      source_label: 'Knowledge base', ref: '3 changes', project_name: 'Acme Widgets',
      entries: [
        { type: 'decision', text: 'A person reviewing work done with a key they minted is not independent.', by: 'Atlas', at: iso(-120) },
        { type: 'decision', text: 'Release ticket links go through one project-scoped insert.', by: 'Atlas', at: iso(-1440) },
        { type: 'pattern', text: 'Every miss returns the same 404 body, service keys included.', by: 'Sentinel', at: iso(-2 * 1440) },
      ],
      facts: [['Changes this week', '3'], ['Decisions', '2']],
      people: [{ name: 'Atlas', role: 'persona', agent: true, you: false }, { name: 'Sentinel', role: 'persona', agent: true, you: false }],
    },
  }),
  item({
    item_key: 'fy_proof1', kind: 'proof', group: 'lately', band: 'lately', involvement: 'member',
    title: 'The loop caught 2 harmful bugs on your work this week',
    why: 'Weekly proof · cost per accepted change 0.4M tokens',
    source: { type: 'weekly_proof', id: '2026-W45' }, link: '/review-loops', project_id: null,
    actions: ['open', 'hide'],
    context: {
      source_label: 'Weekly proof', ref: '2026-W45',
      proof: {
        caught: 2, accepted_changes: 7, rounds: 11, tokens_per_accepted_change: 400000,
        days: [
          { date: day(-4), rounds: 2, caught: 0 }, { date: day(-3), rounds: 3, caught: 1 },
          { date: day(-2), rounds: 2, caught: 0 }, { date: day(-1), rounds: 4, caught: 1 },
        ],
      },
      dollars_visible: false,
      facts: [['Review rounds', '11'], ['Accepted changes', '7'], ['Harmful caught', '2']],
    },
  }),
];

export const fyCollab: CollabProject[] = [
  {
    project_id: FY_PROJECT,
    project_name: 'Acme Widgets',
    group_name: 'Release train',
    people: [
      { name: 'Kemi Ade', role: 'owns tk_0b7d3e9a51c24f60', agent: false, you: false },
      { name: 'Dara Okon', role: 'reviewer', agent: false, you: false },
      { name: 'codex-reviewer', role: 'agent, reviews', agent: true, you: false },
      { name: 'Atlas', role: 'agent, implements', agent: true, you: false },
    ],
    people_count: 2,
    agent_count: 2,
    flows: [
      {
        flow_key: 'fyc_dep1', state: 'dependency', label: 'Waiting on tk_0b7d3e9a51c24f60 to be done first',
        ticket: { id: 'tk_e540d17057a84d14', title: 'Rotated service keys keep the original member’s identity', status: 'open', link: `/projects/${FY_PROJECT}?tab=work&sub=tickets&ticket=tk_e540d17057a84d14` },
        blocking: { id: 'tk_0b7d3e9a51c24f60', title: 'Migration 118 backfill for pre-118 keys', status: 'in_progress', since: iso(-2 * 1440), owner_role: 'owner', link: `/projects/${FY_PROJECT}?tab=work&sub=tickets&ticket=tk_0b7d3e9a51c24f60` },
        since: iso(-2 * 1440),
        chain: [
          { kind: 'mine', ref: 'tk_e540d17057a84d14', title: 'Rotated service keys', status: 'open', since: iso(-3 * 1440), stale: false, who: { name: 'You', agent: false, you: true } },
          { kind: 'dep', ref: 'tk_0b7d3e9a51c24f60', title: 'Migration 118 backfill', status: 'in_progress', since: iso(-2 * 1440), stale: false, who: { name: 'Kemi Ade', agent: false, you: false } },
          { kind: 'dep2', ref: 'tk_3c11a8f04e2b9d77', title: 'Schema review for api_keys', status: 'open', since: iso(-6 * 1440), stale: true, who: { name: 'Dara Okon', agent: false, you: false } },
        ],
        to: { name: 'Kemi Ade', first: 'Kemi' }, can_remind: true,
        default_message: 'Hi Kemi, tk_e540d17057a84d14 (Rotated service keys keep the original member’s identity) is waiting on tk_0b7d3e9a51c24f60, Migration 118 backfill for pre-118 keys. Is it still on track? Happy to help if something is in the way. Thanks!',
      },
      {
        flow_key: 'fyc_rev1', state: 'review', label: 'Waiting for a reviewer',
        ticket: { id: 'tk_9a31c2e07b5d4f18', title: 'docs: self-hosted reminders guide', status: 'review', link: `/projects/${FY_PROJECT}?tab=work&sub=tickets&ticket=tk_9a31c2e07b5d4f18` },
        round: 2, last_verdict: 'CHANGES_REQUESTED', open_findings: 0, reviewer_persona: 'codex-reviewer',
        since: iso(-17 * 60),
        chain: [
          { kind: 'mine', ref: 'tk_9a31c2e07b5d4f18', title: 'Self-hosted reminders guide', status: 'review', since: iso(-17 * 60), stale: false, who: { name: 'You', agent: false, you: true } },
          { kind: 'rev', ref: 'Round 2', title: 'Review', status: 'requested', since: iso(-17 * 60), stale: false, who: { name: 'codex-reviewer (Dara Okon)', agent: false, you: false } },
        ],
        to: { name: 'Dara Okon', first: 'Dara' }, can_remind: true,
        default_message: 'Hi Dara, tk_9a31c2e07b5d4f18 (docs: self-hosted reminders guide) is ready for review round 2. Could you take a look when you get a chance? Thanks!',
      },
    ],
  },
];

function groupsOf(items: AttentionItem[]): AttentionFeed['groups'] {
  const out = { radar: 0, waiting: 0, asking: 0, meetings: 0, coming: 0, second: 0, lately: 0 };
  for (const i of items) out[i.group] += 1;
  return out;
}

const SECTIONS = {
  reminders: { count: 2, error: null }, tickets: { count: 2, error: null }, loop_needs_you: { count: 1, error: null },
  harmful_findings: { count: 1, error: null }, conversation_actions: { count: 3, error: null },
  approvals: { count: 1, error: null },
  questions: { count: 1, error: null }, handoffs: { count: 1, error: null },
  knowledge_changes: { count: 1, error: null }, proof: { count: 1, error: null },
};

export function fyFeed(items: AttentionItem[] = fyItems, collab: CollabProject[] = fyCollab): AttentionFeed {
  const groups = groupsOf(items);
  const bands = { now: 0, your_turn: 0, upcoming: 0, lately: 0 };
  for (const i of items) bands[i.band] += 1;
  return {
    items,
    counts: { ...bands, total: items.length, actionable: groups.radar + groups.waiting + groups.asking },
    groups,
    sections: SECTIONS,
    truncated: false,
    chyron: items
      .filter((i) => i.group !== 'lately')
      .slice(0, 12)
      .map((i) => ({
        item_key: i.item_key,
        category: i.kind === 'loop_harmful_findings' || i.why.includes('overdue')
          ? 'urgent'
          : i.kind === 'approval' ? 'approval'
          : i.group === 'asking' ? 'mention' : i.group === 'meetings' ? 'meeting' : i.kind === 'reminder' ? 'reminder' : i.group === 'second' ? 'forgotten' : 'ticket',
        text: i.title,
        ref: i.context.ref ?? i.source.id,
      })),
    collaboration: { projects: collab, waiting: collab.reduce((n, p) => n + p.flows.length, 0), error: null },
  };
}

/** Nothing actionable: only coming-up, second-look and what-changed items. */
export function fyCaughtUpFeed(): AttentionFeed {
  return fyFeed(fyItems.filter((i) => !['radar', 'waiting', 'asking'].includes(i.group)), []);
}

export const fyItemContext = {
  item_key: 'fy_ticket1',
  ticket: { id: 'tk_e540d17057a84d14', project_id: FY_PROJECT, title: 'Rotated service keys keep the original member’s identity', status: 'open' },
  knowledge: [
    { id: 101, entry_type: 'decision', content: 'A service key is its minter. Same-minter keys count as one identity, and an unresolvable minter fails closed.', created_at: iso(-2 * 1440) },
    { id: 102, entry_type: 'pattern', content: 'Identity checks go through services/review_state.load_key_minters.', created_at: iso(-4 * 1440) },
  ],
  sessions: [
    { id: 'ses_fixture_1', title: 'Self-review: a service key is its minter', source_tool: 'claude-code', updated_at: iso(-2 * 1440), link: '/sessions/ses_fixture_1' },
  ],
};

/** GET /projects/{p}/loops for the Raise limit panel (fy_loop1). */
export const fyLoops = [
  {
    id: 'tk_loop_fixture_01',
    title: 'Dunning emails',
    children: [
      {
        ticket_id: 'tk_4d4e47ca4892414d', title: 'Dunning emails use the org’s locale', status: 'in_progress',
        branch: 'feat/dunning-locale', head_sha: 'b41c9e0', round: 6, harmful_open: 0, findings_by_harm: {}, rounds: [],
        tokens: 1_420_000, budget: { tokens: 2_000_000, max_rounds: 6 }, needs_human: 'round cap hit (6 of 6)',
        ready: false, verdict: null, not_ready_reasons: ['round cap hit'],
      },
    ],
  },
];
