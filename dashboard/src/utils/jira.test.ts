import { describe, expect, it } from 'vitest';
import {
  canUnlinkTicket,
  hasJiraEnterpriseTier,
  isJiraOrgMember,
  JIRA_REWRITE_SOURCE_FIELDS,
  JIRA_SOURCE_FIELDS,
  JIRA_TEMPLATES,
  JIRA_VERBATIM_SOURCE_FIELDS,
  jiraErrorCode,
  parseJiraIssueInput,
  parseJiraIssueKey,
  urlHost,
  jiraSourceFieldsForMode,
  jiraTemplateLabel,
  jiraTemplateOptions,
} from './jira';

/**
 * Pure helpers behind the Jira ticket surface. `hasJiraEnterpriseTier` gates
 * the Promote button; `canUnlinkTicket` mirrors the server's project-admin
 * rule using the full org membership (not the primary-org `me.org_id`).
 */

describe('hasJiraEnterpriseTier', () => {
  it('is true when the project tier is enterprise, even for a Team caller', () => {
    expect(
      hasJiraEnterpriseTier(
        { effective_tier: 'enterprise' },
        { tier: 'team', effective_tier: 'team' },
      ),
    ).toBe(true);
  });

  it('falls back to the caller tier when the project omits effective_tier', () => {
    expect(
      hasJiraEnterpriseTier(
        { effective_tier: null },
        { tier: 'enterprise', effective_tier: 'enterprise' },
      ),
    ).toBe(true);
  });

  it('is false when both project and caller tiers are lower', () => {
    expect(
      hasJiraEnterpriseTier(
        { effective_tier: 'team' },
        { tier: 'pro', effective_tier: 'pro' },
      ),
    ).toBe(false);
  });

  it('does not promote a legacy admin on a non-enterprise project', () => {
    // A newer server reports the project's own tier; the platform-admin bypass
    // must not leak onto a project the backend's `check_project_feature` would
    // refuse with a 403.
    expect(
      hasJiraEnterpriseTier(
        { effective_tier: 'team' },
        { tier: 'admin', effective_tier: 'enterprise' },
      ),
    ).toBe(false);
  });

  it('keeps the legacy admin fallback when the project omits effective_tier', () => {
    // Older servers omit the project's effective_tier; tier:'admin' still maps
    // to enterprise so legacy accounts are not locked out.
    expect(
      hasJiraEnterpriseTier(
        { effective_tier: null },
        { tier: 'admin', effective_tier: 'enterprise' },
      ),
    ).toBe(true);
  });

  it('is false when both project and me are missing', () => {
    expect(hasJiraEnterpriseTier(null, null)).toBe(false);
    expect(hasJiraEnterpriseTier(undefined, undefined)).toBe(false);
  });
});

describe('canUnlinkTicket', () => {
  const project = { owner_id: 'owner_1', org_id: 'org_x' };

  it('is true for the project owner', () => {
    expect(canUnlinkTicket(project, { user_id: 'owner_1' }, [])).toBe(true);
  });

  it('is true for an admin of the project org, resolved from full membership', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [
      { org_id: 'org_primary', role: 'member' },
      { org_id: 'org_x', role: 'admin' },
    ];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(true);
  });

  it('is true for an owner of the project org', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [{ org_id: 'org_x', role: 'owner' }];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(true);
  });

  it('does not treat primary-org role as the project org role', () => {
    // me is an admin of their *primary* org, but only a member of the
    // project's org — they must not be able to unlink.
    const me = { user_id: 'member_1' };
    const myOrgs = [
      { org_id: 'org_primary', role: 'admin' },
      { org_id: 'org_x', role: 'member' },
    ];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(false);
  });

  it('is false for a member of the project org', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [{ org_id: 'org_x', role: 'member' }];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(false);
  });

  it('is false when the user has no membership in the project org', () => {
    const me = { user_id: 'member_1' };
    const myOrgs = [{ org_id: 'org_y', role: 'admin' }];
    expect(canUnlinkTicket(project, me, myOrgs)).toBe(false);
  });

  it('is false when there is no org scope and the user is not the owner', () => {
    expect(
      canUnlinkTicket({ owner_id: 'owner_1', org_id: null }, { user_id: 'member_1' }, []),
    ).toBe(false);
  });

  it('is false when me, project, or membership is missing', () => {
    expect(canUnlinkTicket(null, { user_id: 'owner_1' }, [])).toBe(false);
    expect(canUnlinkTicket(project, null, [])).toBe(false);
    expect(canUnlinkTicket(project, { user_id: 'member_1' }, undefined)).toBe(false);
  });
});

describe('jiraErrorCode', () => {
  it('returns undefined for non-ApiError values', () => {
    expect(jiraErrorCode(new Error('boom'))).toBeUndefined();
    expect(jiraErrorCode('nope')).toBeUndefined();
  });
});

describe('isJiraOrgMember', () => {
  it('is true for any member of the project org, whatever the role', () => {
    expect(isJiraOrgMember({ org_id: 'org_x' }, [{ org_id: 'org_x' }])).toBe(true);
  });

  it('is false for non-members, personal projects and while orgs load', () => {
    expect(isJiraOrgMember({ org_id: 'org_x' }, [{ org_id: 'org_y' }])).toBe(false);
    expect(isJiraOrgMember({ org_id: null }, [{ org_id: 'org_x' }])).toBe(false);
    expect(isJiraOrgMember({ org_id: 'org_x' }, undefined)).toBe(false);
  });
});

describe('jiraTemplateOptions', () => {
  const custom = (slug: string, name: string, archived: boolean) => ({
    slug,
    name,
    builtin: false,
    archived_at: archived ? '2026-01-01T00:00:00Z' : null,
  });

  it('lists the built-ins including the Design/UX template', () => {
    expect(JIRA_TEMPLATES).toContainEqual({ value: 'design', label: 'Design / UX' });
    const options = jiraTemplateOptions();
    expect(options).toHaveLength(5);
    expect(options.map((o) => o.value)).toEqual(['agile', 'compliance', 'sre', 'bug', 'design']);
  });

  it('appends active custom templates after the built-ins and drops archived ones', () => {
    const options = jiraTemplateOptions([
      custom('design-lite', 'Design Lite', false),
      custom('old-tpl', 'Old', true),
    ]);
    expect(options).toEqual([
      { value: 'agile', label: 'Agile Standard' },
      { value: 'compliance', label: 'Enterprise / Compliance' },
      { value: 'sre', label: 'Platform / SRE' },
      { value: 'bug', label: 'Bug Report' },
      { value: 'design', label: 'Design / UX' },
      { value: 'design-lite', label: 'Design Lite' },
    ]);
  });

  it('keeps the current archived value selectable for that field only', () => {
    const templates = [custom('design-lite', 'Design Lite', false), custom('old-tpl', 'Old', true)];
    const forField = jiraTemplateOptions(templates, 'old-tpl');
    expect(forField).toContainEqual({ value: 'old-tpl', label: 'Old (archived)' });
    // Another field whose value isn't the archived slug never sees it.
    expect(jiraTemplateOptions(templates, 'agile').map((o) => o.value)).not.toContain('old-tpl');
    expect(jiraTemplateOptions(templates).map((o) => o.value)).not.toContain('old-tpl');
  });

  it('lets an org custom template shadow a built-in with the same slug', () => {
    const options = jiraTemplateOptions([custom('bug', 'Our Bug', false)]);
    expect(options.filter((o) => o.value === 'bug')).toEqual([{ value: 'bug', label: 'Our Bug' }]);
  });
});

describe('jiraSourceFieldsForMode', () => {
  it('offers only fields with a verbatim rendering for verbatim sections', () => {
    expect(jiraSourceFieldsForMode('verbatim').map((f) => f.value)).toEqual([
      ...JIRA_VERBATIM_SOURCE_FIELDS,
    ]);
    expect(jiraSourceFieldsForMode('verbatim').map((f) => f.value)).not.toContain('title');
  });

  it('offers the whole vocabulary for rewrite sections', () => {
    expect(jiraSourceFieldsForMode('rewrite').map((f) => f.value)).toEqual([
      ...JIRA_REWRITE_SOURCE_FIELDS,
    ]);
    expect(JIRA_REWRITE_SOURCE_FIELDS).toEqual(JIRA_SOURCE_FIELDS.map((f) => f.value));
  });
});

describe('jiraTemplateLabel', () => {
  it('resolves built-in slugs and falls back to the raw slug when unknown', () => {
    expect(jiraTemplateLabel('design')).toBe('Design / UX');
    expect(jiraTemplateLabel('agile')).toBe('Agile Standard');
    expect(jiraTemplateLabel('made-up')).toBe('made-up');
    expect(jiraTemplateLabel(null)).toBe('—');
  });

  it('resolves custom template names by slug', () => {
    expect(
      jiraTemplateLabel('design-lite', [
        { slug: 'design-lite', name: 'Design Lite', builtin: false, archived_at: null },
      ]),
    ).toBe('Design Lite');
  });

  it('marks an archived custom template', () => {
    expect(
      jiraTemplateLabel('old', [
        { slug: 'old', name: 'Old', builtin: false, archived_at: '2026-01-01T00:00:00Z' },
      ]),
    ).toBe('Old (archived)');
  });
});

describe('JIRA_SOURCE_FIELDS', () => {
  it('exposes the full 8-field source vocabulary', () => {
    expect(JIRA_SOURCE_FIELDS.map((f) => f.value)).toEqual([
      'title',
      'description',
      'acceptance_criteria',
      'priority',
      'kind',
      'persona',
      'completion_notes',
      'linked_sessions',
    ]);
  });
});

describe('parseJiraIssueKey', () => {
  it.each([
    ['ABC-123', 'ABC-123'],
    ['  abc-123  ', 'ABC-123'],
    ['EMMS_2-9', 'EMMS_2-9'],
    ['https://acme.atlassian.net/browse/ABC-123', 'ABC-123'],
    ['https://acme.atlassian.net/browse/abc-7?focusedCommentId=1#c', 'ABC-7'],
    ['https://acme.atlassian.net/browse/ABC-123/', 'ABC-123'],
    ['https://acme.atlassian.net/jira/software/projects/ABC/boards/1?selectedIssue=ABC-42', 'ABC-42'],
    ['https://acme.atlassian.net/jira/software/projects/ABC/issues/ABC-123', 'ABC-123'],
    ['https://acme.atlassian.net/jira/servicedesk/projects/SD/queues/custom/1/sd-12', 'SD-12'],
    ['acme.atlassian.net/browse/ABC-9', 'ABC-9'],
    ['jira.example.com:8443/browse/OPS-1', 'OPS-1'],
  ])('reads %s as %s', (input, key) => {
    expect(parseJiraIssueKey(input)).toBe(key);
  });

  it.each([
    '',
    '   ',
    'ABC',
    '123-ABC',
    'ABC-0',
    'ABC-01',
    '1BC-1',
    'AB C-1',
    'ABC-12345678901',
    `A${'B'.repeat(50)}-1`,
    'https://acme.atlassian.net/jira/your-work',
    'https://acme.atlassian.net/jira/software/projects/ABC/issues',
    'acme.atlassian.net/jira/your-work',
    'ftp://acme.atlassian.net/browse/ABC-1',
    'https://acme.atlassian.net/browse/not a key',
    'not a url/browse/ABC-1',
  ])('rejects %j', (input) => {
    expect(parseJiraIssueKey(input)).toBeNull();
  });
});

describe('parseJiraIssueInput', () => {
  it('reports the pasted host, or null for a bare key', () => {
    expect(parseJiraIssueInput('abc-1')).toEqual({ key: 'ABC-1', host: null });
    expect(parseJiraIssueInput('https://Other.atlassian.net/browse/ABC-1')).toEqual({ key: 'ABC-1', host: 'other.atlassian.net' });
    expect(parseJiraIssueInput('acme.atlassian.net/browse/ABC-1')).toEqual({ key: 'ABC-1', host: 'acme.atlassian.net' });
  });
});

describe('urlHost', () => {
  it('returns the host of http(s) URLs only', () => {
    expect(urlHost('https://Acme.atlassian.net/browse/A-1')).toBe('acme.atlassian.net');
    expect(urlHost('javascript:alert(1)')).toBeNull();
    expect(urlHost('not a url')).toBeNull();
    expect(urlHost(null)).toBeNull();
  });
});
