import type { MarketplaceDisclosure } from '../api/client';

const CONSENT_COPY: Record<string, string> = {
  'skill-scripts': 'scripts',
  'skill-hooks': 'hooks and pre-approved tools',
  'executable-extensions': 'executable extensions',
  'hidden-unicode': 'hidden characters',
  'scan-findings': 'scanner findings',
  'mcp-review': 'MCP servers',
};

export interface TrustAlert {
  key: string;
  tone: 'danger' | 'warning';
  text: string;
}

/** The disclosure's warnings, in plain language, for the default view. */
export function disclosureAlerts(disclosure: MarketplaceDisclosure): TrustAlert[] {
  const alerts: TrustAlert[] = [];
  const unicodeCounts = disclosure.unicode?.counts || {};
  const unicodeTotal = Object.values(unicodeCounts).reduce((sum, value) => sum + Number(value || 0), 0);
  if ((disclosure.unicode?.findings || []).length > 0 || unicodeTotal > 0 || disclosure.unicode?.truncated) {
    const files = disclosure.unicode?.flagged_files ?? 0;
    const found = (disclosure.unicode?.findings || []).length > 0 || unicodeTotal > 0 || files > 0;
    alerts.push({
      key: 'unicode',
      tone: 'danger',
      text: found
        ? `Hidden characters found${files > 0 ? ` in ${files} file${files === 1 ? '' : 's'}` : ''}${disclosure.unicode?.truncated ? ' (scan truncated)' : ''}. Check Install details before installing.`
        : 'Hidden-character scan was truncated, so some files were not checked. Check Install details before installing.',
    });
  }
  if ((disclosure.incomplete || []).length > 0) {
    alerts.push({ key: 'incomplete', tone: 'danger', text: `Not fully analyzed: ${(disclosure.incomplete || []).join(', ')}.` });
  }
  const severity = disclosure.scan?.max_severity;
  if (severity === 'critical' || severity === 'high') {
    alerts.push({ key: 'scan', tone: severity === 'critical' ? 'danger' : 'warning', text: `The scanner found a ${severity} severity issue.` });
  }
  const consent = (disclosure.consent_required || []).filter((item) => item !== 'instructions');
  if (consent.length > 0) {
    alerts.push({ key: 'consent', tone: 'warning', text: `Installing asks for your consent to: ${consent.map((item) => CONSENT_COPY[item] || item).join(', ')}.` });
  }
  return alerts;
}

export function scanSummary(disclosure: MarketplaceDisclosure): string {
  const scan = disclosure.scan || {};
  const count = scan.finding_count || 0;
  const findings = count === 0 ? 'no open findings' : `${count} finding${count === 1 ? '' : 's'}${scan.max_severity ? `, worst ${scan.max_severity}` : ''}`;
  return `Scanned${scan.ruleset ? ` with ruleset ${scan.ruleset}` : ''}: ${findings}${scan.truncated ? ' (scan truncated)' : ''}.`;
}

