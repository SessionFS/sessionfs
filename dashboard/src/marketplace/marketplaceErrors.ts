/**
 * Plain-language explanations for marketplace publish / report / moderation
 * failures. The publish wording mirrors the CLI (`sfs skill publish`,
 * src/sessionfs/skills/publishing.py `explain_failure` and
 * publish_validation.py `REASON_HINTS`) so both surfaces say the same thing.
 * Never render raw JSON: unknown failures fall back to the server message.
 */
import { ApiError } from '../api/client';

export const MAX_UPLOAD_BYTES = 48 * 1024 * 1024 + 10_000 * 1024;
export const MAX_UPLOAD_MIB = Math.floor(MAX_UPLOAD_BYTES / (1024 * 1024));

/** Same patterns as publish_validation.NAMESPACE_RE / COLLECTION_RE. */
export const NAMESPACE_RE = /^[a-z0-9](?:[a-z0-9.-]{0,98}[a-z0-9])?$/;
export const COLLECTION_RE = /^[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?$/;

export const NAMESPACE_HELP =
  "1-100 lowercase letters, digits, '.' or '-', starting and ending with a letter or digit.";
export const COLLECTION_HELP =
  "1-100 lowercase letters, digits, '.', '_' or '-', starting and ending with a letter or digit.";

const AGENT_PLUGIN_SCHEMA = 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json';

/** publish_validation.REASON_HINTS, verbatim where it does not depend on a local path. */
export const REASON_HINTS: Record<string, string> = {
  package_too_large:
    `Keep the compressed upload under ${MAX_UPLOAD_MIB} MiB and the expanded package under 48 MiB and 5000 files; move large assets out of the package.`,
  member_too_large: 'Every file must be at most 16 MiB; split or remove the file.',
  too_many_members: 'An archive may hold at most 10000 entries; remove files.',
  invalid_archive: 'Upload a gzip-compressed tar (.tar.gz). Let `sfs skill publish <dir>` build it for you.',
  extended_header:
    'Hosted packages must be plain ustar/GNU tar without PAX or long-name records: keep each path within 100 bytes for the file name and 155 bytes for its directory, and pack with `sfs skill publish <dir>` (or `tar --format=ustar`).',
  unsafe_path: "Use relative paths inside the package only: no absolute paths, '..', or backslashes.",
  non_portable_path:
    'Rename the path: no Windows-reserved names (con, nul, aux, com1...), no trailing dot or space, and none of < > : " | ? * or control characters.',
  path_collision:
    'Two paths differ only by letter case or Unicode normalization; rename one so they stay distinct on macOS and Windows.',
  non_regular_file: 'Packages may contain only regular files and directories; replace links with real files.',
  symlink: 'Symlinks are not allowed in packages; replace each link with the file it points to.',
  path_too_long:
    'Shorten the path: hosted archives are ustar, so a file name may be at most 100 bytes and its directory at most 155 bytes.',
  missing_manifest:
    `Add a plugin.json with "$schema": "${AGENT_PLUGIN_SCHEMA}", "name", "version" and "description", plus a skills/<name>/SKILL.md for each skill.`,
  plugin_manifest:
    `Fix plugin.json: it must be a JSON object targeting "${AGENT_PLUGIN_SCHEMA}" with a lowercase "name", a non-empty "description" and a short printable "version".`,
  skill_missing: 'Every directory under skills/ needs a SKILL.md with YAML frontmatter.',
  skill_frontmatter:
    "SKILL.md must start with a '---' line, then YAML with `name` (matching the directory) and `description`, then a '---' line at column 0. Use plain keys and values: no anchors, aliases, tags, duplicate keys, or NEL/U+2028/U+2029 characters.",
  skill_name:
    "Use 1-64 lowercase letters, digits and '-' for skill names, with no consecutive '-'; the name must match the skill's directory.",
  reserved_name_case: 'Spell reserved files exactly (plugin.json, SKILL.md, mcp.json); case matters on Linux.',
  mcp_config:
    'Fix the MCP declaration: mcpServers must be an object, URLs must be plain http(s) without embedded credentials, and secrets must come from the environment at runtime.',
  mcp_credentials:
    'Remove credential-like literals from the MCP configuration; reference environment variables (for example ${API_TOKEN}) that users set at install time.',
  instruction_not_utf8: 'Save instruction files (.md, .txt) as UTF-8.',
  instruction_bundle_too_large:
    'Instruction text (.md/.txt inside skills) must total at most 256 KiB; move reference material into fewer or shorter files.',
  dlp_scan_limit: 'Text files must be at most 16 MiB to be scanned; split the file.',
  dlp_findings:
    'Remove the secret or personal data from the reported lines (rotate any real credential), then validate again. Example values must not look like real keys.',
  license_required:
    'Add a "license" field to plugin.json (for example "MIT", "Apache-2.0", or "UNLICENSED" for internal-only work).',
  invalid_package: 'Fix the reported problem and validate again.',
};

export interface Envelope {
  status: number;
  code: string | null;
  /** Server message WITHOUT the "code: " prefix ApiError adds for toasts. */
  message: string;
  details: Record<string, unknown>;
}

/** Normalize any thrown value into status/code/message/details. */
export function envelopeOf(error: unknown): Envelope {
  if (!(error instanceof ApiError)) {
    return { status: 0, code: null, message: error instanceof Error ? error.message : String(error), details: {} };
  }
  let message = error.message;
  let code: string | null = error.code ?? null;
  const details: Record<string, unknown> = { ...(error.details || {}) };
  try {
    const parsed = JSON.parse(error.raw) as Record<string, unknown>;
    const env = parsed?.error as Record<string, unknown> | undefined;
    if (env && typeof env === 'object') {
      if (typeof env.message === 'string') message = env.message;
    } else if (parsed && typeof parsed.detail === 'object' && parsed.detail) {
      // Bare FastAPI `detail` dict (scope errors use `error` + `message`).
      const d = parsed.detail as Record<string, unknown>;
      if (typeof d.message === 'string') message = d.message;
      if (!code && typeof d.code === 'string') code = d.code;
      Object.assign(details, d);
    }
  } catch {
    // Non-JSON body: ApiError already holds its text.
  }
  // Plain-string HTTPExceptions arrive with code === "<status>"; that is not
  // a meaningful code for explanation purposes.
  if (code && /^\d{3}$/.test(code)) code = null;
  return { status: error.status, code, message, details };
}

function isScopeError(env: Envelope): boolean {
  return env.code === 'insufficient_scope' || env.details.error === 'insufficient_scope' || env.message.includes('Scope required');
}

function humanBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}

export interface Explanation {
  /** What happened. */
  title: string;
  /** What to do about it (may be empty). */
  fix: string;
  /** Stable code for tests / support, e.g. "invalid_skill_package/dlp_findings". */
  code: string | null;
}

export interface PublishTargetInfo {
  kind: 'personal' | 'org';
  orgName?: string;
  collection: string;
}

/** Port of publishing.explain_failure for the dashboard upload form. */
export function explainPublishError(error: unknown, target: PublishTargetInfo): Explanation {
  const env = envelopeOf(error);
  const { status, code, message, details } = env;
  const reason = typeof details.reason === 'string' ? details.reason : '';
  const tag = code ? (reason ? `${code}/${reason}` : code) : status ? String(status) : null;
  const make = (title: string, fix = ''): Explanation => ({ title, fix, code: tag });
  const retryAfter = details.retry_after;

  if (status === 0) return make(`The upload did not reach the server: ${message || 'network error'}.`, 'Check your connection and retry; nothing was published.');
  if (status === 401) {
    return make('Your session was rejected (missing, revoked or expired API key).', 'Sign out and sign in again.');
  }
  if (status === 403) {
    if (isScopeError(env)) {
      const scope = target.kind === 'org' ? 'skills:admin' : 'marketplace:write';
      return make(`This API key lacks the ${scope} scope.`, `Sign in with a user key or a key granted ${scope}.`);
    }
    const feature = details.feature;
    if (code === 'upgrade_required' && feature === 'marketplace_access') {
      const tier = typeof details.current_tier === 'string' ? details.current_tier : 'your current';
      return make(
        `Personal publishing needs the Starter plan or above (you are on ${tier}).`,
        'Upgrade your plan, or publish into an organization whose plan includes the team skill library.',
      );
    }
    if (code === 'upgrade_required' && feature === 'team_skill_library') {
      const tier = typeof details.current_tier === 'string' ? details.current_tier : 'its current';
      return make(
        `${target.orgName || 'This organization'} is on the ${tier} plan, which does not include the team skill library.`,
        'Ask an org owner to upgrade, or publish to your personal library instead.',
      );
    }
    if (code === 'upgrade_required') return make(message || 'Your plan does not include publishing.', 'Upgrade your plan.');
    if (message.includes('admin or owner')) return make(`${message}.`, 'Ask an org owner to publish it or to make you an admin.');
    if (message.includes('platform administrator')) return make(`${message}.`, 'Publish with organization visibility instead.');
    if (message.includes('Service keys')) return make(`${message}.`, 'Publish personal packages with a user API key.');
    return make(message || 'The server refused the publish (403).', 'Check your role and plan.');
  }
  if (status === 404) return make(message || 'Not found.', 'You must be a member of that organization.');
  if (status === 408 || code === 'publish_timeout') {
    return make(message || 'The upload did not reach the server in time.', 'Retry on a faster connection; the whole upload must arrive within two minutes.');
  }
  if (status === 409 && code === 'visibility_mismatch') {
    const existing = typeof details.existing_visibility === 'string' ? details.existing_visibility : 'another visibility';
    return make(
      message,
      `This exact version is already listed as '${existing}' in collection '${target.collection}'. Publish into another collection or delete that listing.`,
    );
  }
  if (status === 413) {
    if (code === 'storage_limit') {
      const used = details.current_usage;
      const limit = details.limit;
      const size = details.package_bytes;
      const extra = typeof used === 'number' && typeof limit === 'number' && typeof size === 'number'
        ? ` (using ${humanBytes(used)} of ${humanBytes(limit)}; this package needs ${humanBytes(size)})`
        : '';
      return make(`Publishing would exceed the storage quota${extra}.`, 'Delete old package versions or synced sessions, or upgrade the plan.');
    }
    return make(message || 'The package upload is too large.', REASON_HINTS.package_too_large);
  }
  if (status === 429) {
    if (code === 'personal_skill_version_limit' || code === 'skill_package_version_limit') {
      return make(message, 'Delete older versions of the package and retry.');
    }
    return make(message || 'Too many requests.', retryAfter ? `Retry after ${String(retryAfter)} seconds.` : 'Retry shortly.');
  }
  if (status === 503 && code === 'publish_busy') {
    return make(
      'The server is busy publishing other packages (or you already have a publish in flight).',
      `Retry in ${retryAfter ? `${String(retryAfter)} seconds` : 'a few seconds'}: nothing was published.`,
    );
  }
  if (status === 422) {
    if (code === 'license_required') return make(message, REASON_HINTS.license_required);
    if (code === 'invalid_skill_package') {
      let fix: string;
      if (reason === 'dlp_findings' && target.kind === 'org') {
        fix = "Your organization's DLP policy (custom patterns) may have flagged this content. Run `sfs skill validate` locally for the file and line, and ask an org admin for the policy (`sfs dlp policy`).";
      } else if (reason && REASON_HINTS[reason]) {
        fix = REASON_HINTS[reason];
      } else {
        fix = 'Run `sfs skill validate` on the same source for the file and line.';
      }
      return make(`The server rejected the package: ${message}`, fix);
    }
    return make(message || 'The server rejected the request (422).', 'Check the namespace and collection fields.');
  }
  if (status >= 500) return make(`Server error (${status}): ${message || 'no detail'}.`, 'Retry later; nothing was published.');
  return make(`Publish failed (${status}): ${message || 'no detail'}.`);
}

export function explainReportError(error: unknown): Explanation {
  const env = envelopeOf(error);
  const tag = env.code || (env.status ? String(env.status) : null);
  if (env.status === 409 && env.code === 'report_already_open') {
    return { title: 'You already have an open report for this package.', fix: 'Moderators will review it; you can file another once it is resolved.', code: tag };
  }
  if (env.status === 429) {
    return { title: 'Too many reports right now.', fix: 'You can file up to 10 reports a minute and 20 a day. Try again later.', code: tag };
  }
  if (env.status === 404) {
    return { title: 'This package is no longer available to report.', fix: 'It may already have been yanked, blocked or removed.', code: tag };
  }
  if (env.code === 'service_key_not_allowed') {
    return { title: 'Reports must be filed with a user key.', fix: 'Sign in with your own account.', code: tag };
  }
  if (env.status === 403 && isScopeError(env)) {
    return { title: 'This API key lacks the marketplace:write scope.', fix: 'Sign in with a user key.', code: tag };
  }
  return { title: env.message || 'The report could not be filed.', fix: '', code: tag };
}

export function explainModerationError(error: unknown): Explanation {
  const env = envelopeOf(error);
  const tag = env.code || (env.status ? String(env.status) : null);
  const current = typeof env.details.current_state === 'string' ? env.details.current_state : null;
  if (env.code === 'already_revoked') {
    return { title: env.message || 'The target is already taken down.', fix: current ? `Current state: ${current}.` : '', code: tag };
  }
  if (env.code === 'not_revoked') {
    return { title: 'Nothing to restore: the target is not yanked or blocked.', fix: '', code: tag };
  }
  if (env.code === 'findings_required') {
    return {
      title: 'Restoring this quarantine clears its critical scan findings.',
      fix: 'Review each finding listed below and confirm that every one is a false positive.',
      code: tag,
    };
  }
  if (env.code === 'unknown_finding') {
    return { title: 'A finding is no longer open on this version.', fix: 'Refresh and review the findings again.', code: tag };
  }
  if (env.code === 'report_not_open') {
    return { title: 'The report has already been resolved.', fix: 'Refresh the queue.', code: tag };
  }
  if (env.code === 'moderation_forbidden' || env.status === 403) {
    if (isScopeError(env)) return { title: 'This API key lacks the skills:admin scope.', fix: 'Sign in with your own user key.', code: tag };
    return {
      title: env.message || 'You cannot moderate this listing.',
      fix: 'Public listings need a platform admin; org listings need an owner or admin of that org; only a platform admin can undo a platform admin takedown.',
      code: tag,
    };
  }
  if (env.status === 404) return { title: 'Not found, or not visible to you.', fix: 'It may have been removed, or it belongs to a scope you do not moderate.', code: tag };
  if (env.status === 422) return { title: env.message || 'The request was rejected.', fix: 'A reason or note with visible text is required (max 500 characters).', code: tag };
  return { title: env.message || 'The action failed.', fix: '', code: tag };
}
