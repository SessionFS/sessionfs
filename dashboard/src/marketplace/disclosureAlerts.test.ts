import { describe, expect, it } from 'vitest';
import type { MarketplaceDisclosure } from '../api/client';
import { disclosureAlerts } from './disclosureAlerts';

function card(unicode: MarketplaceDisclosure['unicode']): MarketplaceDisclosure {
  return {
    version_id: 'v', content_hash: 'h', capability_digest: 'c', capabilities: ['instructions'],
    provenance: { publisher_verified: true, attested: false }, scan: { finding_count: 0 }, unicode,
    shell_commands: [], network_endpoints: [], env_names: [], mcp_servers: { local: [], remote: [], declared: false },
    allowed_tools: [], persistence_targets: [], executable_extensions: [], tools: {}, consent_required: ['instructions'],
  };
}

describe('disclosureAlerts hidden characters', () => {
  it('says the scan was truncated instead of "found in 0 files"', () => {
    const alerts = disclosureAlerts(card({ flagged_files: 0, findings: [], counts: {}, truncated: true }));
    expect(alerts).toHaveLength(1);
    expect(alerts[0].text).toMatch(/^Hidden-character scan was truncated/);
    expect(alerts[0].text).not.toMatch(/0 files/);
  });

  it('counts flagged files and notes truncation when characters were found', () => {
    const alerts = disclosureAlerts(card({ flagged_files: 2, findings: [], counts: { bidi: 3 }, truncated: true }));
    expect(alerts[0].text).toBe('Hidden characters found in 2 files (scan truncated). Check Install details before installing.');
  });

  it('raises nothing for a clean scan', () => {
    expect(disclosureAlerts(card({ flagged_files: 0, findings: [], counts: {} }))).toEqual([]);
  });
});
