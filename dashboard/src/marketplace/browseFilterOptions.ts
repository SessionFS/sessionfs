import { MARKETPLACE_TOOLS, type MarketplaceFilterState } from '../hooks/useMarketplace';

export const TOOL_OPTIONS = [
  { value: '', label: 'All tools' },
  ...MARKETPLACE_TOOLS.map((tool) => ({ value: tool, label: tool })),
];

export const SORT_OPTIONS = [
  { value: 'relevance', label: 'Best match' },
  { value: 'recent', label: 'Newest' },
];

export const SEVERITY_OPTIONS = [
  { value: '', label: 'Any result' },
  { value: 'none', label: 'No open findings' },
  { value: 'low', label: 'Low severity at most' },
  { value: 'medium', label: 'Medium severity at most' },
  { value: 'high', label: 'High severity at most' },
];
export const CAPABILITY_OPTIONS = [
  { value: '', label: 'Anything' },
  { value: 'instructions_only', label: 'Instructions only' },
  { value: 'scripts', label: 'Scripts' },
  { value: 'mcp', label: 'MCP servers' },
  { value: 'hooks', label: 'Hooks' },
  { value: 'executables', label: 'Executable extensions' },
  { value: 'allowed_tools', label: 'Pre-approved tools' },
];
export const UPDATED_OPTIONS = [
  { value: '', label: 'Any time' },
  { value: '7', label: 'Past 7 days' },
  { value: '30', label: 'Past 30 days' },
  { value: '90', label: 'Past 90 days' },
  { value: '365', label: 'Past year' },
];
export const WORKS_IN_OPTIONS = [{ value: '', label: 'Any tool' }, ...MARKETPLACE_TOOLS.map((tool) => ({ value: tool, label: tool }))];

const labelOf = (options: { value: string; label: string }[], value: string) =>
  options.find((option) => option.value === value)?.label ?? value;

/** The filters that live behind the Filters button (everything except the
 *  search, tool, sort and category controls that stay on the page). */
export type PanelKey = 'max_severity' | 'capability' | 'works_in' | 'updated_within' | 'license' | 'verified' | 'attested' | 'include_deprecated';

export const CLEARED_PANEL_FILTERS: Pick<MarketplaceFilterState, PanelKey> = {
  max_severity: undefined,
  capability: undefined,
  works_in: undefined,
  updated_within: undefined,
  license: undefined,
  verified: undefined,
  attested: undefined,
  include_deprecated: undefined,
};

/** One removable chip per active panel filter, in panel order. */
export function activeFilterChips(filters: MarketplaceFilterState): { key: PanelKey; label: string }[] {
  const chips: { key: PanelKey; label: string }[] = [];
  if (filters.max_severity) chips.push({ key: 'max_severity', label: `Scan: ${labelOf(SEVERITY_OPTIONS, filters.max_severity).toLowerCase()}` });
  if (filters.capability) chips.push({ key: 'capability', label: `Includes: ${labelOf(CAPABILITY_OPTIONS, filters.capability).toLowerCase()}` });
  if (filters.works_in) chips.push({ key: 'works_in', label: `Works in ${filters.works_in}` });
  if (filters.updated_within) chips.push({ key: 'updated_within', label: `Updated: ${labelOf(UPDATED_OPTIONS, String(filters.updated_within)).toLowerCase()}` });
  if (filters.license) chips.push({ key: 'license', label: `License: ${filters.license}` });
  if (filters.verified) chips.push({ key: 'verified', label: 'Verified publishers' });
  if (filters.attested) chips.push({ key: 'attested', label: 'Published from CI' });
  if (filters.include_deprecated === false) chips.push({ key: 'include_deprecated', label: 'Hiding deprecated' });
  return chips;
}

