import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import {
  confluenceErrorCode,
  confluenceFriendlyError,
  confluenceTemplateLabel,
  confluenceTemplateOptions,
  hasConfluenceEnterpriseTier,
  isConfluenceOrgMember,
} from './confluence';

/**
 * Pure helpers behind the Confluence publish surface. `hasConfluenceEnterpriseTier`
 * gates the publish controls; `isConfluenceOrgMember` mirrors the server's
 * org_membership_required rule; the error-code helpers translate the v0.10.x
 * envelope into user-facing text.
 */

describe('confluenceTemplateOptions', () => {
  it('lists exactly the five built-in templates', () => {
    expect(confluenceTemplateOptions().map((t) => t.value)).toEqual([
      'reference',
      'architecture',
      'runbook',
      'onboarding',
      'concept',
    ]);
  });
});

describe('confluenceTemplateLabel', () => {
  it('labels a built-in slug and falls back for unknown/empty', () => {
    expect(confluenceTemplateLabel('runbook')).toBe('Runbook');
    expect(confluenceTemplateLabel('nope')).toBe('nope');
    expect(confluenceTemplateLabel(null)).toBe('—');
  });
});

describe('confluenceErrorCode', () => {
  it('reads the semantic code from details.error, falling back to the status code', () => {
    const drifted = new ApiError(
      409,
      JSON.stringify({
        error: { code: '409', message: 'conflict', details: { error: 'confluence_page_drifted' } },
      }),
    );
    expect(confluenceErrorCode(drifted)).toBe('confluence_page_drifted');

    const upstream = new ApiError(502, JSON.stringify({ error: { code: '502', message: 'bad gateway' } }));
    expect(confluenceErrorCode(upstream)).toBe('502');
    expect(confluenceErrorCode(new Error('boom'))).toBeUndefined();
  });
});

describe('confluenceFriendlyError', () => {
  function envelope(code: string, status = 400): ApiError {
    return new ApiError(
      status,
      JSON.stringify({ error: { code: String(status), message: code, details: { error: code } } }),
    );
  }

  it('maps known codes to actionable copy', () => {
    expect(confluenceFriendlyError(envelope('confluence_not_connected'))).toMatch(/connected Confluence/i);
    expect(confluenceFriendlyError(envelope('confluence_mapping_required'))).toMatch(/mapping/i);
    expect(confluenceFriendlyError(envelope('confluence_page_drifted'))).toMatch(/edited since/i);
    expect(confluenceFriendlyError(envelope('content_too_large'))).toMatch(/too large/i);
  });

  it('falls back to the envelope message for unknown codes', () => {
    expect(confluenceFriendlyError(envelope('wat'))).toMatch(/wat/i);
  });
});

describe('hasConfluenceEnterpriseTier', () => {
  it('is true when the project tier is enterprise, even for a Team caller', () => {
    expect(
      hasConfluenceEnterpriseTier(
        { effective_tier: 'enterprise' },
        { tier: 'team', effective_tier: 'team' },
      ),
    ).toBe(true);
  });

  it('falls back to the caller tier when the project omits effective_tier', () => {
    expect(
      hasConfluenceEnterpriseTier(
        { effective_tier: null },
        { tier: 'enterprise', effective_tier: 'enterprise' },
      ),
    ).toBe(true);
  });

  it('is false when both project and caller tiers are lower', () => {
    expect(
      hasConfluenceEnterpriseTier(
        { effective_tier: 'team' },
        { tier: 'pro', effective_tier: 'pro' },
      ),
    ).toBe(false);
  });

  it('does not promote a legacy admin on a non-enterprise project', () => {
    expect(
      hasConfluenceEnterpriseTier(
        { effective_tier: 'team' },
        { tier: 'admin', effective_tier: 'enterprise' },
      ),
    ).toBe(false);
  });
});

describe('isConfluenceOrgMember', () => {
  it('is true when the project org is in the caller membership', () => {
    expect(isConfluenceOrgMember({ org_id: 'org_x' }, [{ org_id: 'org_x' }])).toBe(true);
  });

  it('is false for non-members, personal projects, and while membership loads', () => {
    expect(isConfluenceOrgMember({ org_id: 'org_x' }, [{ org_id: 'org_y' }])).toBe(false);
    expect(isConfluenceOrgMember({ org_id: null }, [{ org_id: 'org_x' }])).toBe(false);
    expect(isConfluenceOrgMember({ org_id: 'org_x' }, null)).toBe(false);
  });
});
