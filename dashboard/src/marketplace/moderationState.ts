/**
 * Role / tier helpers and revocation-state derivation for the marketplace.
 * The server re-derives authority on every request; these only decide what
 * the UI offers.
 */
import type { MarketplacePackage, ReportReason, RevocationEvent } from '../api/client';

export const REPORT_REASON_LABELS: Record<ReportReason, string> = {
  malicious: 'Malicious: harmful instructions, scripts or MCP servers',
  secrets: 'Leaks secrets or personal data',
  impersonation: 'Impersonates another publisher or product',
  broken: 'Broken or does not work as described',
  license: 'License problem or copied without permission',
  spam: 'Spam or low-quality duplicate',
  other: 'Something else',
};

const ROLE_LEVEL: Record<string, number> = { member: 10, admin: 50, owner: 100 };

/** Mirrors server roles.has_minimum_role: owners always count as admins. */
export function hasMinimumRole(role: string | null | undefined, required: 'member' | 'admin' | 'owner'): boolean {
  if (!role) return false;
  const level = ROLE_LEVEL[role];
  return level !== undefined && level >= ROLE_LEVEL[required];
}

/** Tiers whose feature set includes `marketplace_access` (server tiers.py). */
const PERSONAL_PUBLISH_TIERS = new Set(['starter', 'pro', 'team', 'enterprise']);

export interface MeLike {
  tier?: string | null;
  effective_tier?: string | null;
  org_id?: string | null;
  org_role?: string | null;
}

export function isPlatformAdmin(me: MeLike | undefined | null): boolean {
  return me?.tier === 'admin';
}

export function canPublishPersonal(me: MeLike | undefined | null): boolean {
  if (!me) return false;
  if (isPlatformAdmin(me)) return true;
  return PERSONAL_PUBLISH_TIERS.has(me.effective_tier || me.tier || '');
}

export interface OrgMembership {
  org_id: string;
  name: string;
  role?: string;
}

export function adminOrgs(orgs: OrgMembership[] | undefined, me: MeLike | undefined | null): OrgMembership[] {
  if (isPlatformAdmin(me)) return orgs || [];
  return (orgs || []).filter((org) => hasMinimumRole(org.role, 'admin'));
}

/** Platform admins and owners/admins of any org see the moderation queue. */
export function canModerate(me: MeLike | undefined | null, orgs?: OrgMembership[]): boolean {
  if (isPlatformAdmin(me)) return true;
  if (hasMinimumRole(me?.org_role, 'admin')) return true;
  return (orgs || []).some((org) => hasMinimumRole(org.role, 'admin'));
}

/**
 * `quarantined`: an automated instruction-scan rescan stopped NEW installs of a
 * version (platform layer). Existing installs are warned, never removed.
 */
export type ModerationState = 'active' | 'yanked' | 'quarantined' | 'blocked';

/** One hash-wide content block, kept per (hash, block scope, scope org). */
export interface ContentBlock {
  contentHash: string;
  state: ModerationState;
  blockScope: string | null;
  /** Org the block is limited to (block_scope "org"); null/undefined = unscoped or older server. */
  scopeOrgId?: string | null;
}

export interface RevocationIndex {
  packages: Map<string, ModerationState>;
  versions: Map<string, ModerationState>;
  /** Content blocks (`kind: "content"`), keyed by `hash|scope|org`, apart from versions. */
  contents?: Map<string, ContentBlock>;
  /**
   * Active revocation layers per target (`v:<id>` / `p:<id>`): `platform` and
   * `owner:<org>` are independent; `versions`/`packages` hold the worst of them.
   */
  layers?: Map<string, Map<string, RevocationLayer>>;
}

export interface RevocationLayer {
  state: ModerationState;
  blockScope: string | null;
  scopeOrgId: string | null;
}

/**
 * An ORG-scoped entry (version, package or content) applies only to a copy
 * the org owns AND that comes from a non-public listing: an org block never
 * reaches a public copy. Unknown owner / visibility (older server) or no
 * scope_org_id: applies, cautiously.
 */
export function scopeApplies(
  blockScope: string | null | undefined,
  scopeOrgId: string | null | undefined,
  ownerOrgId: string | null | undefined,
  visibility: string | null | undefined,
): boolean {
  const visibilityUnknown = visibility === undefined || visibility === null;
  if (blockScope === 'owner') {
    // Owner-layer action (a publisher's own yank/restore): only copies from a
    // NON-PUBLIC listing; with scope_org_id also only that org's copies.
    if (scopeOrgId && ownerOrgId !== undefined && ownerOrgId !== scopeOrgId) return false;
    return visibilityUnknown || visibility !== 'public';
  }
  if (blockScope !== 'org' || !scopeOrgId) return true;
  if (ownerOrgId === undefined) return true;
  if (ownerOrgId !== scopeOrgId) return false;
  if (visibilityUnknown) return true;
  return visibility !== 'public';
}

/**
 * The feed could not be read to its end (page cap hit). The query FAILS with
 * this instead of caching a partial replay, so the previous complete index
 * stays in use and nothing is gated on truncated state.
 */
export class RevocationFeedIncompleteError extends Error {
  constructor() {
    super('The revocations feed could not be read to its end');
    this.name = 'RevocationFeedIncompleteError';
  }
}

export function isRevocationFeedIncomplete(error: unknown): boolean {
  return error instanceof RevocationFeedIncompleteError;
}

const SEVERITY: Record<ModerationState, number> = { active: 0, yanked: 1, quarantined: 2, blocked: 3 };

function toState(state: RevocationEvent['state']): ModerationState | null {
  if (state === 'restored') return 'active';
  if (state === 'yanked') return 'yanked';
  if (state === 'blocked') return 'blocked';
  if (state === 'quarantined') return 'quarantined';
  return null; // unknown future state: ignore
}

/** Replay the feed (oldest first): the latest event per target wins. */
export function buildRevocationIndex(events: RevocationEvent[]): RevocationIndex {
  const packages = new Map<string, ModerationState>();
  const versions = new Map<string, ModerationState>();
  const contents = new Map<string, ContentBlock>();
  const layers = new Map<string, Map<string, RevocationLayer>>();
  for (const event of events) {
    const state = toState(event.state);
    if (state === null) continue;
    if (event.kind === 'content') {
      if (!event.content_hash) continue;
      const blockScope = event.block_scope ?? null;
      const scopeOrgId = blockScope === 'org' || blockScope === 'owner' ? event.scope_org_id ?? null : null;
      // An org's restore never lifts another org's (or the global) block.
      contents.set(`${event.content_hash}|${blockScope ?? ''}|${scopeOrgId ?? ''}`, {
        contentHash: event.content_hash,
        state,
        blockScope,
        scopeOrgId,
      });
    } else if (event.kind !== undefined && event.kind !== 'version' && event.kind !== 'package') {
      continue; // unknown kind from a newer server
    } else if (event.version_id && event.kind !== 'package') {
      applyLayer(layers, `v:${event.version_id}`, event, state);
      versions.set(event.version_id, worstLayer(layers.get(`v:${event.version_id}`)));
    } else if (event.package_id) {
      applyLayer(layers, `p:${event.package_id}`, event, state);
      packages.set(event.package_id, worstLayer(layers.get(`p:${event.package_id}`)));
    }
  }
  return { packages, versions, contents, layers };
}

/**
 * Version/package state is kept PER LAYER: platform transitions (block_scope
 * global/null) and owner-layer transitions (org/owner, per org) are
 * independent. A restore clears only its own layer; a target is revoked
 * while any layer is active.
 */
function applyLayer(
  layers: Map<string, Map<string, RevocationLayer>>,
  target: string,
  event: RevocationEvent,
  state: ModerationState,
): void {
  const blockScope = event.block_scope ?? null;
  const ownerLayer = blockScope === 'org' || blockScope === 'owner';
  if (state === 'quarantined' && ownerLayer) return; // quarantine is platform-only
  const scopeOrgId = ownerLayer ? event.scope_org_id ?? null : null;
  // A quarantine is its own platform flag: a yank or block never overwrites it.
  const layerKey = ownerLayer ? `owner:${scopeOrgId ?? ''}` : state === 'quarantined' ? 'quarantine' : 'platform';
  const byLayer = layers.get(target) ?? new Map<string, RevocationLayer>();
  layers.set(target, byLayer);
  if (state === 'active') {
    byLayer.delete(layerKey);
    // A platform restore covers the whole platform layer; the server re-emits
    // whatever platform state remains right after it.
    if (!ownerLayer) byLayer.delete('quarantine');
  } else byLayer.set(layerKey, { state, blockScope, scopeOrgId });
}

function worstLayer(byLayer: Map<string, RevocationLayer> | undefined, filter?: (layer: RevocationLayer) => boolean): ModerationState {
  let result: ModerationState = 'active';
  for (const layer of byLayer?.values() ?? []) {
    if (filter && !filter(layer)) continue;
    if (SEVERITY[layer.state] > SEVERITY[result]) result = layer.state;
  }
  return result;
}

/**
 * Most severe content block that applies to a package with these bytes. An
 * org-scoped block applies only to a package owned by that org; an unknown
 * owner or a block without scope_org_id (older server) applies as before.
 */
export function contentState(
  index: RevocationIndex | undefined,
  contentHash: string | null | undefined,
  ownerOrgId: string | null | undefined,
  visibility?: string | null,
): ModerationState {
  let result: ModerationState = 'active';
  if (!contentHash || !index?.contents) return result;
  for (const block of index.contents.values()) {
    if (block.contentHash !== contentHash) continue;
    if (!scopeApplies(block.blockScope, block.scopeOrgId, ownerOrgId, visibility)) continue;
    if (SEVERITY[block.state] > SEVERITY[result]) result = block.state;
  }
  return result;
}

/** Version/package state of ONE listing, honouring org-scoped entries. */
function listingFeedState(index: RevocationIndex | undefined, item: MarketplacePackage): ModerationState {
  const applies = (layer: RevocationLayer) =>
    scopeApplies(layer.blockScope, layer.scopeOrgId, item.owner_org_id, item.visibility);
  const forTarget = (key: string, fallback: ModerationState | undefined): ModerationState =>
    index?.layers ? worstLayer(index.layers.get(key), applies) : fallback || 'active';
  const own = forTarget(`v:${item.version_id}`, index?.versions.get(item.version_id));
  const pkg = forTarget(`p:${item.package_id}`, index?.packages.get(item.package_id));
  return SEVERITY[own] >= SEVERITY[pkg] ? own : pkg;
}

/** Effective state of one version: the more severe of its own and its package's. */
export function versionState(
  index: RevocationIndex | undefined,
  packageId: string,
  versionId: string | null | undefined,
): ModerationState {
  const own = (versionId && index?.versions.get(versionId)) || 'active';
  const pkg = index?.packages.get(packageId) || 'active';
  return SEVERITY[own] >= SEVERITY[pkg] ? own : pkg;
}

export function packageState(index: RevocationIndex | undefined, packageId: string): ModerationState {
  return index?.packages.get(packageId) || 'active';
}

/** State of a listing row: server fields first, then the revocations feed. */
export function listingState(item: MarketplacePackage, index?: RevocationIndex): ModerationState {
  const fromServer: ModerationState = item.verification_state === 'blocked' || item.moderation_state === 'blocked'
    ? 'blocked'
    : item.is_yanked || item.moderation_state === 'yanked'
      ? 'yanked'
      : 'active';
  const byVersion = listingFeedState(index, item);
  const byContent = contentState(index, item.content_hash, item.owner_org_id, item.visibility);
  const fromFeed = SEVERITY[byVersion] >= SEVERITY[byContent] ? byVersion : byContent;
  return SEVERITY[fromServer] >= SEVERITY[fromFeed] ? fromServer : fromFeed;
}

export const STATE_COPY: Record<Exclude<ModerationState, 'active'>, { label: string; help: string }> = {
  yanked: {
    label: 'yanked',
    help: 'Withdrawn by its publisher or a moderator. It can no longer be downloaded or requested.',
  },
  quarantined: {
    label: 'quarantined',
    help: 'Flagged by an automated security scan. New installs are refused while a moderator reviews it; existing installs are kept.',
  },
  blocked: {
    label: 'blocked',
    help: 'Taken down by a moderator. Downloads are refused and existing grants were revoked.',
  },
};
