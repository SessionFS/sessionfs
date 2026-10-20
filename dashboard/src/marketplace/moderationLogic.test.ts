import { describe, expect, it } from 'vitest';
import { ApiError, type MarketplacePackage, type RevocationEvent } from '../api/client';
import {
  envelopeOf,
  explainModerationError,
  explainPublishError,
  explainReportError,
  REASON_HINTS,
} from './marketplaceErrors';
import {
  RevocationFeedIncompleteError,
  STATE_COPY,
  adminOrgs,
  buildRevocationIndex,
  contentState,
  isRevocationFeedIncomplete,
  canModerate,
  canPublishPersonal,
  hasMinimumRole,
  listingState,
  packageState,
  scopeApplies,
  versionState,
} from './moderationState';

function err(status: number, body: unknown) {
  return new ApiError(status, typeof body === 'string' ? body : JSON.stringify(body));
}

const personal = { kind: 'personal' as const, collection: 'personal' };
const orgTarget = { kind: 'org' as const, orgName: 'Acme', collection: 'org' };

describe('role and tier helpers', () => {
  it('treats owners as admins and never literal-matches "admin"', () => {
    expect(hasMinimumRole('owner', 'admin')).toBe(true);
    expect(hasMinimumRole('admin', 'admin')).toBe(true);
    expect(hasMinimumRole('member', 'admin')).toBe(false);
    expect(hasMinimumRole('platform_admin', 'admin')).toBe(false);
    expect(hasMinimumRole(null, 'member')).toBe(false);
  });

  it('decides who may moderate', () => {
    expect(canModerate({ tier: 'admin' })).toBe(true);
    expect(canModerate({ tier: 'team', org_role: 'owner' })).toBe(true);
    expect(canModerate({ tier: 'team', org_role: 'member' }, [{ org_id: 'o2', name: 'B', role: 'owner' }])).toBe(true);
    expect(canModerate({ tier: 'team', org_role: 'member' }, [{ org_id: 'o1', name: 'A', role: 'member' }])).toBe(false);
    expect(canModerate(undefined)).toBe(false);
  });

  it('gates personal publishing on marketplace_access tiers', () => {
    expect(canPublishPersonal({ tier: 'free', effective_tier: 'free' })).toBe(false);
    expect(canPublishPersonal({ tier: 'free', effective_tier: 'starter' })).toBe(true);
    expect(canPublishPersonal({ tier: 'enterprise' })).toBe(true);
    expect(canPublishPersonal({ tier: 'admin', effective_tier: 'free' })).toBe(true);
    expect(adminOrgs([{ org_id: 'a', name: 'A', role: 'member' }, { org_id: 'b', name: 'B', role: 'owner' }], { tier: 'pro' }).map((o) => o.org_id)).toEqual(['b']);
    expect(adminOrgs([{ org_id: 'a', name: 'A', role: 'member' }], { tier: 'admin' })).toHaveLength(1);
  });
});

describe('revocation index', () => {
  const events: RevocationEvent[] = [
    { package_id: 'p1', version_id: 'v1', content_hash: 'h', state: 'yanked', reason: 'r', changed_at: '1' },
    { package_id: 'p1', version_id: 'v1', content_hash: 'h', state: 'restored', reason: 'r', changed_at: '2' },
    { package_id: 'p1', version_id: 'v2', content_hash: 'h', state: 'blocked', reason: 'r', changed_at: '3' },
    { package_id: 'p2', version_id: null, content_hash: null, state: 'yanked', reason: 'r', changed_at: '4' },
  ];
  const index = buildRevocationIndex(events);

  it('replays events so the latest per target wins', () => {
    expect(versionState(index, 'p1', 'v1')).toBe('active');
    expect(versionState(index, 'p1', 'v2')).toBe('blocked');
    expect(packageState(index, 'p2')).toBe('yanked');
  });

  it('applies package-wide state to every version, keeping the more severe', () => {
    expect(versionState(index, 'p2', 'v9')).toBe('yanked');
    const both = buildRevocationIndex([...events, { package_id: 'p1', version_id: null, content_hash: null, state: 'yanked', reason: '', changed_at: '5' }]);
    expect(versionState(both, 'p1', 'v2')).toBe('blocked');
    expect(versionState(both, 'p1', 'v1')).toBe('yanked');
  });

  it('combines server listing fields with the feed', () => {
    const row = { package_id: 'p3', version_id: 'v3', verification_state: 'verified' } as MarketplacePackage;
    expect(listingState(row, index)).toBe('active');
    expect(listingState({ ...row, verification_state: 'blocked' }, index)).toBe('blocked');
    expect(listingState({ ...row, is_yanked: true }, undefined)).toBe('yanked');
    expect(listingState({ ...row, package_id: 'p2' }, index)).toBe('yanked');
  });

  it('keeps content-hash blocks apart from version state and ignores unknown states', () => {
    const base = { reason: '', block_scope: 'global' as const };
    const index2 = buildRevocationIndex([
      { ...base, package_id: 'p9', version_id: 'v9', content_hash: 'sha256:aa', state: 'blocked', kind: 'version', changed_at: '1' },
      { ...base, package_id: null, version_id: null, content_hash: 'sha256:aa', state: 'blocked', kind: 'content', changed_at: '2' },
      { ...base, package_id: null, version_id: null, content_hash: 'sha256:aa', state: 'restored', kind: 'content', changed_at: '3' },
      { ...base, package_id: 'p9', version_id: 'v9', content_hash: null, state: 'quarantined', kind: 'version', changed_at: '4' },
      { ...base, package_id: 'p9', version_id: null, content_hash: 'sha256:bb', state: 'blocked', kind: 'mystery', changed_at: '5' },
      { ...base, package_id: null, version_id: null, content_hash: 'sha256:cc', state: 'blocked', kind: 'content', changed_at: '6' },
    ]);
    // Content restore clears only the hash-wide block; the version block stays.
    expect(contentState(index2, 'sha256:aa', undefined)).toBe('active');
    expect(versionState(index2, 'p9', 'v9')).toBe('blocked');
    expect(index2.packages.has('p9')).toBe(false);
    const row = { package_id: 'p8', version_id: 'v8', content_hash: 'sha256:cc', verification_state: 'verified' } as MarketplacePackage;
    expect(listingState(row, index2)).toBe('blocked');
  });

  it('shows a quarantine as its own badge state that a later restore clears', () => {
    const base = { reason: '', content_hash: 'sha256:qq', package_id: 'pq', version_id: 'vq', kind: 'version' as const };
    const quarantined = buildRevocationIndex([
      { ...base, state: 'yanked', block_scope: null, changed_at: '1' },
      { ...base, state: 'quarantined', block_scope: 'global', changed_at: '2' },
    ]);
    // Quarantine outranks a yank and does not overwrite it.
    expect(versionState(quarantined, 'pq', 'vq')).toBe('quarantined');
    const row = { package_id: 'pq', version_id: 'vq', verification_state: 'verified' } as MarketplacePackage;
    expect(listingState(row, quarantined)).toBe('quarantined');
    expect(STATE_COPY.quarantined.label).toBe('quarantined');
    // Lifting it: restore, then the server re-emits the remaining yank.
    const lifted = buildRevocationIndex([
      { ...base, state: 'yanked', block_scope: null, changed_at: '1' },
      { ...base, state: 'quarantined', block_scope: 'global', changed_at: '2' },
      { ...base, state: 'restored', block_scope: 'global', changed_at: '3' },
      { ...base, state: 'yanked', block_scope: null, changed_at: '4' },
    ]);
    expect(versionState(lifted, 'pq', 'vq')).toBe('yanked');
    const cleared = buildRevocationIndex([
      { ...base, state: 'quarantined', block_scope: 'global', changed_at: '1' },
      { ...base, state: 'restored', block_scope: 'global', changed_at: '2' },
    ]);
    expect(versionState(cleared, 'pq', 'vq')).toBe('active');
    // An owner-scoped quarantine is malformed and ignored.
    const owner = buildRevocationIndex([
      { ...base, state: 'quarantined', block_scope: 'org', scope_org_id: 'org_a', changed_at: '1' },
    ]);
    expect(versionState(owner, 'pq', 'vq')).toBe('active');
  });

  it('applies an org-scoped content block only to packages owned by that org', () => {
    const block = {
      package_id: null, version_id: null, content_hash: 'sha256:dd', reason: '', kind: 'content',
    } as const;
    const index3 = buildRevocationIndex([
      { ...block, state: 'blocked', block_scope: 'org', scope_org_id: 'org_a', changed_at: '1' },
      // Another org's restore of the same bytes never lifts org_a's block.
      { ...block, state: 'restored', block_scope: 'org', scope_org_id: 'org_b', changed_at: '2' },
    ]);
    const row = { package_id: 'p1', version_id: 'v1', content_hash: 'sha256:dd', verification_state: 'verified' } as MarketplacePackage;
    expect(listingState({ ...row, owner_org_id: 'org_a' }, index3)).toBe('blocked');
    expect(listingState({ ...row, owner_org_id: 'org_b' }, index3)).toBe('active');
    expect(listingState({ ...row, owner_org_id: null }, index3)).toBe('active'); // personal / public
    expect(listingState(row, index3)).toBe('blocked'); // older server: owner unknown, as before
    // A global block (or one without scope_org_id) reaches every owner.
    const index4 = buildRevocationIndex([
      { ...block, state: 'blocked', block_scope: 'global', changed_at: '1' },
    ]);
    expect(listingState({ ...row, owner_org_id: 'org_b' }, index4)).toBe('blocked');
  });

  it('never lets an org block reach a public copy (version, package or content)', () => {
    const scoped = { reason: '', block_scope: 'org' as const, scope_org_id: 'org_a', changed_at: '1', state: 'blocked' as const };
    const index5 = buildRevocationIndex([
      { ...scoped, package_id: 'p5', version_id: 'v5', content_hash: null, kind: 'version' },
      { ...scoped, package_id: 'p6', version_id: null, content_hash: null, kind: 'package' },
      { ...scoped, package_id: null, version_id: null, content_hash: 'sha256:ee', kind: 'content' },
    ]);
    const base = { verification_state: 'verified', owner_org_id: 'org_a' } as MarketplacePackage;
    const rows = [
      { ...base, package_id: 'p5', version_id: 'v5', content_hash: 'sha256:x1' },
      { ...base, package_id: 'p6', version_id: 'v6', content_hash: 'sha256:x2' },
      { ...base, package_id: 'p7', version_id: 'v7', content_hash: 'sha256:ee' },
    ] as MarketplacePackage[];
    for (const row of rows) {
      // The org's own non-public copy is blocked ...
      expect(listingState({ ...row, visibility: 'org' }, index5)).toBe('blocked');
      // ... a public copy of it never is ...
      expect(listingState({ ...row, visibility: 'public' }, index5)).toBe('active');
      // ... nor another org's copy; an unknown owner stays cautious.
      expect(listingState({ ...row, visibility: 'org', owner_org_id: 'org_b' }, index5)).toBe('active');
      expect(listingState({ ...row, visibility: 'org', owner_org_id: undefined }, index5)).toBe('blocked');
    }
    expect(scopeApplies('org', 'org_a', 'org_a', undefined)).toBe(true); // visibility unknown: cautious
    expect(scopeApplies('global', null, 'org_b', 'public')).toBe(true);
  });

  it('applies owner-layer yanks only to copies from a non-public listing', () => {
    const yank = { reason: '', changed_at: '1', state: 'yanked' as const, kind: 'version', content_hash: null };
    const index6 = buildRevocationIndex([
      // The org yanks its private listing of v8; a personal owner yanks v9.
      { ...yank, package_id: 'p8', version_id: 'v8', block_scope: 'owner', scope_org_id: 'org_a' },
      { ...yank, package_id: 'p9', version_id: 'v9', block_scope: 'owner', scope_org_id: null },
    ]);
    const org = { package_id: 'p8', version_id: 'v8', content_hash: 'sha256:a8', verification_state: 'verified', owner_org_id: 'org_a' } as MarketplacePackage;
    expect(listingState({ ...org, visibility: 'org' }, index6)).toBe('yanked');
    // The SAME version from the public listing is not affected.
    expect(listingState({ ...org, visibility: 'public' }, index6)).toBe('active');
    expect(listingState({ ...org, visibility: 'org', owner_org_id: 'org_b' }, index6)).toBe('active');
    const personal = { package_id: 'p9', version_id: 'v9', content_hash: 'sha256:a9', verification_state: 'verified', owner_org_id: null } as MarketplacePackage;
    expect(listingState({ ...personal, visibility: 'user' }, index6)).toBe('yanked');
    expect(listingState({ ...personal, visibility: 'public' }, index6)).toBe('active');
    expect(scopeApplies('owner', null, undefined, undefined)).toBe(true); // unknown: cautious
  });

  it('keeps platform and owner layers independent (both orders)', () => {
    const base = { package_id: 'pL', version_id: 'vL', content_hash: null, reason: '', kind: 'version' };
    const row = { package_id: 'pL', version_id: 'vL', content_hash: 'sha256:l', verification_state: 'verified', owner_org_id: 'org_a', visibility: 'org' } as MarketplacePackage;
    // Owner yank, platform block, platform restore -> the owner yank remains.
    const first = buildRevocationIndex([
      { ...base, state: 'yanked', block_scope: 'owner', scope_org_id: 'org_a', changed_at: '1' },
      { ...base, state: 'blocked', block_scope: 'global', changed_at: '2' },
      { ...base, state: 'restored', block_scope: 'global', changed_at: '3' },
    ]);
    expect(listingState(row, first)).toBe('yanked');
    expect(versionState(first, 'pL', 'vL')).toBe('yanked');
    // Platform yank, owner yank, owner restore -> the platform yank remains.
    const second = buildRevocationIndex([
      { ...base, state: 'yanked', block_scope: null, changed_at: '1' },
      { ...base, state: 'yanked', block_scope: 'owner', scope_org_id: 'org_a', changed_at: '2' },
      { ...base, state: 'restored', block_scope: 'owner', scope_org_id: 'org_a', changed_at: '3' },
    ]);
    expect(listingState(row, second)).toBe('yanked');
    expect(listingState({ ...row, visibility: 'public' }, second)).toBe('yanked'); // platform: everyone
    // Both layers restored -> active.
    const cleared = buildRevocationIndex([
      { ...base, state: 'yanked', block_scope: null, changed_at: '1' },
      { ...base, state: 'restored', block_scope: null, changed_at: '2' },
    ]);
    expect(listingState(row, cleared)).toBe('active');
  });

  it('marks only the truncation error as an incomplete feed', () => {
    expect(isRevocationFeedIncomplete(new RevocationFeedIncompleteError())).toBe(true);
    expect(isRevocationFeedIncomplete(new Error('network'))).toBe(false);
  });
});

describe('envelopeOf', () => {
  it('strips the "code: " prefix and folds plain-status codes away', () => {
    const env = envelopeOf(err(422, { error: { code: 'invalid_skill_package', message: 'bad tar', details: { reason: 'symlink' } } }));
    expect(env).toEqual({ status: 422, code: 'invalid_skill_package', message: 'bad tar', details: { reason: 'symlink' } });
    expect(envelopeOf(err(403, { error: { code: '403', message: 'Organization admin or owner role required', details: {} } })).code).toBeNull();
    expect(envelopeOf(new Error('offline')).status).toBe(0);
  });

  it('reads a bare FastAPI detail dict (scope errors)', () => {
    const env = envelopeOf(err(403, { detail: { error: 'insufficient_scope', message: 'Scope required: one of [x].' } }));
    expect(env.details.error).toBe('insufficient_scope');
    expect(env.message).toBe('Scope required: one of [x].');
  });
});

describe('explainPublishError', () => {
  it('maps every reason code to its CLI hint', () => {
    for (const reason of Object.keys(REASON_HINTS)) {
      const result = explainPublishError(err(422, { error: { code: 'invalid_skill_package', message: 'm', details: { reason } } }), personal);
      expect(result.fix).toBe(REASON_HINTS[reason]);
      expect(result.code).toBe(`invalid_skill_package/${reason}`);
    }
  });

  it('points org DLP findings at the org policy', () => {
    const result = explainPublishError(err(422, { error: { code: 'invalid_skill_package', message: 'm', details: { reason: 'dlp_findings' } } }), orgTarget);
    expect(result.fix).toMatch(/organization's DLP policy/);
  });

  it('explains license, role, scope, storage, version caps and timeouts', () => {
    expect(explainPublishError(err(422, { error: { code: 'license_required', message: 'm', details: { reason: 'license_required' } } }), orgTarget).fix).toBe(REASON_HINTS.license_required);
    expect(explainPublishError(err(403, { error: { code: '403', message: 'Organization admin or owner role required', details: {} } }), orgTarget).fix).toMatch(/make you an admin/);
    expect(explainPublishError(err(403, { detail: { error: 'insufficient_scope', message: 'Scope required' } }), orgTarget).title).toMatch(/skills:admin/);
    expect(explainPublishError(err(403, { error: { code: 'upgrade_required', message: 'm', details: { feature: 'team_skill_library', current_tier: 'pro' } } }), orgTarget).title)
      .toBe('Acme is on the pro plan, which does not include the team skill library.');
    expect(explainPublishError(err(413, { error: { code: 'storage_limit', message: 'm', details: { current_usage: 1024, limit: 2048, package_bytes: 4096 } } }), personal).title)
      .toBe('Publishing would exceed the storage quota (using 1.0 KiB of 2.0 KiB; this package needs 4.0 KiB).');
    expect(explainPublishError(err(429, { error: { code: 'personal_skill_version_limit', message: 'limit', details: {} } }), personal).fix).toMatch(/Delete older versions/);
    expect(explainPublishError(err(408, { error: { code: 'publish_timeout', message: 'slow', details: {} } }), personal).fix).toMatch(/two minutes/);
    expect(explainPublishError(err(500, 'Internal Server Error'), personal).title).toBe('Server error (500): Internal Server Error.');
  });
});

describe('report and moderation explanations', () => {
  it('handles report conflicts, rate limits and missing packages', () => {
    expect(explainReportError(err(409, { error: { code: 'report_already_open', message: 'x', details: {} } })).title).toMatch(/already have an open report/);
    expect(explainReportError(err(429, { error: { code: 'report_rate_limited', message: 'x', details: {} } })).title).toBe('Too many reports right now.');
    expect(explainReportError(err(404, { error: { code: '404', message: 'Marketplace package not found', details: {} } })).title).toMatch(/no longer available/);
  });

  it('handles moderation conflicts and 422 reasons', () => {
    expect(explainModerationError(err(409, { error: { code: 'not_revoked', message: 'x', details: { current_state: 'active' } } })).title).toMatch(/Nothing to restore/);
    expect(explainModerationError(err(409, { error: { code: 'report_not_open', message: 'x', details: {} } })).title).toMatch(/already been resolved/);
    expect(explainModerationError(err(422, { error: { code: '422', message: 'reason must contain visible text', details: {} } })).fix).toMatch(/visible text/);
  });
});
