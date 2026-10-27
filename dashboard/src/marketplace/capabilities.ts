import type { MarketplacePackage } from '../api/client';

type Risk = MarketplacePackage['risk'];

/** Everything beyond plain instructions that a package can ship. Each one
 *  asks for explicit consent at install time. Order is the display order. */
export const CAPABILITIES: { key: string; label: string; present: (risk: Risk) => boolean; detail: (risk: Risk) => string }[] = [
  { key: 'scripts', label: 'Scripts', present: (r) => !!r.scripts, detail: () => '' },
  { key: 'mcp', label: 'MCP servers', present: (r) => !!r.mcp, detail: () => '' },
  { key: 'executables', label: 'Executable extensions', present: (r) => !!r.executable_extensions, detail: () => '' },
  { key: 'hooks', label: 'Hooks', present: (r) => !!r.hooks, detail: (r) => (r.hook_events || []).join(', ') },
  { key: 'allowed_tools', label: 'Pre-approved tools', present: (r) => (r.allowed_tools || []).length > 0, detail: (r) => (r.allowed_tools || []).join(', ') },
];

export function elevatedCapabilities(risk: Risk) {
  return CAPABILITIES.filter((capability) => capability.present(risk));
}

/** One short, consistent capability label for a package card. */
export function capabilitySummary(risk: Risk): { elevated: boolean; label: string; title: string } {
  const present = elevatedCapabilities(risk);
  if (present.length === 0) {
    return {
      elevated: false,
      label: 'Instructions only',
      title: 'Only instruction files: no scripts, MCP servers, hooks, executables or pre-approved tools.',
    };
  }
  const names = present.map((capability) => capability.label.toLowerCase());
  return {
    elevated: true,
    label: `Includes ${names[0]}${present.length > 1 ? ` +${present.length - 1}` : ''}`,
    title: `Installing asks for your consent to: ${names.join(', ')}.`,
  };
}

export const VISIBILITY_LABELS: Record<string, string> = {
  public: 'Public',
  org: 'Organization only',
  user: 'Only you',
};

/** Active publisher with at least one live verification badge. */
export function isVerifiedPublisher(item: MarketplacePackage): boolean {
  const publisher = item.publisher;
  return !!publisher && publisher.status === 'active' && publisher.verified && publisher.badges.length > 0;
}
