import type { SessionSummary } from '../../api/client';
import { friendlyModelName, fullToolName, isUnknownModelId } from '../../utils/models';

export interface SessionSummaryWithAudit extends SessionSummary {
  audit_trust_score?: number | null;
}

export type SortKey = 'date' | 'messages' | 'tokens' | 'title' | 'tool' | 'model';
export type SortDir = 'desc' | 'asc';

export interface LineageGroup {
  root: SessionSummary;
  children: SessionSummary[];
}

export function groupByLineage(sessions: SessionSummary[]): LineageGroup[] {
  const byId = new Map(sessions.map(s => [s.id, s]));

  // Find the root of each session's chain (walk up parent links)
  function findRoot(id: string, visited = new Set<string>()): string {
    const s = byId.get(id);
    if (!s || !s.parent_session_id || !byId.has(s.parent_session_id) || visited.has(id)) return id;
    visited.add(id);
    return findRoot(s.parent_session_id, visited);
  }

  // Group all sessions by their root
  const rootToChildren = new Map<string, SessionSummary[]>();
  for (const s of sessions) {
    const rootId = findRoot(s.id);
    if (rootId === s.id) continue; // this IS the root
    if (!rootToChildren.has(rootId)) rootToChildren.set(rootId, []);
    rootToChildren.get(rootId)!.push(s);
  }

  // Build groups: roots with all descendants, ordered by updated_at
  const childIds = new Set<string>();
  for (const kids of rootToChildren.values()) {
    for (const k of kids) childIds.add(k.id);
  }

  const groups: LineageGroup[] = [];
  for (const s of sessions) {
    if (!childIds.has(s.id)) {
      const kids = rootToChildren.get(s.id) || [];
      kids.sort((a, b) => new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime());
      groups.push({ root: s, children: kids });
    }
  }
  return groups;
}

export function findDuplicateGroups(sessions: SessionSummary[]): Map<string, SessionSummary[]> {
  const groups = new Map<string, SessionSummary[]>();
  for (const s of sessions) {
    // Strong key: title + message_count + source_tool + etag (content hash)
    // etag is the SHA-256 of the session archive — identical content = identical etag
    const key = `${s.title || ''}:${s.message_count}:${s.source_tool}:${(s as SessionSummary & { etag?: string }).etag || ''}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(s);
  }
  const dupes = new Map<string, SessionSummary[]>();
  for (const [key, group] of groups) {
    if (group.length >= 2) dupes.set(key, group);
  }
  return dupes;
}

export function isToday(dateStr: string): boolean {
  const d = new Date(dateStr);
  const now = new Date();
  return d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
}

export function isThisWeek(dateStr: string): boolean {
  const d = new Date(dateStr);
  const now = new Date();
  const weekAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7);
  return d >= weekAgo && !isToday(dateStr);
}

function compareByDate(a: SessionSummary, b: SessionSummary): number {
  return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
}

function compareByMessages(a: SessionSummary, b: SessionSummary): number {
  return b.message_count - a.message_count;
}

function compareByTokens(a: SessionSummary, b: SessionSummary): number {
  return (
    b.total_input_tokens + b.total_output_tokens -
    (a.total_input_tokens + a.total_output_tokens)
  );
}

function compareByTitle(a: SessionSummary, b: SessionSummary): number {
  const titleCmp = (a.title || '').localeCompare(b.title || '');
  return titleCmp !== 0 ? titleCmp : compareByDate(a, b);
}

function compareByTool(a: SessionSummary, b: SessionSummary): number {
  const toolCmp = fullToolName(a.source_tool).localeCompare(fullToolName(b.source_tool));
  return toolCmp !== 0 ? toolCmp : compareByDate(a, b);
}

/** Mirrors the server's `sort=model`: model id asc/desc, the unknown bucket
 *  (null, "", "unknown", "<synthetic>") LAST in both directions, ties newest
 *  first. Used where the list isn't server-paginated (folders). */
function compareByModel(dir: SortDir) {
  return (a: SessionSummary, b: SessionSummary): number => {
    const aUnknown = isUnknownModelId(a.model_id);
    const bUnknown = isUnknownModelId(b.model_id);
    if (aUnknown !== bUnknown) return aUnknown ? 1 : -1;
    if (!aUnknown) {
      const am = a.model_id as string;
      const bm = b.model_id as string;
      // Code-point order, like the database's default collation for ids.
      const modelCmp = am < bm ? -1 : am > bm ? 1 : 0;
      if (modelCmp !== 0) return dir === 'asc' ? modelCmp : -modelCmp;
    }
    return compareByDate(a, b);
  };
}

export function sortSessions(list: SessionSummary[], sortBy: SortKey, sortDir: SortDir = 'desc'): SessionSummary[] {
  const sorted = [...list];
  // Model sort applies its own direction so unknown models stay last.
  if (sortBy === 'model') return sorted.sort(compareByModel(sortDir));
  if (sortBy === 'messages') sorted.sort(compareByMessages);
  else if (sortBy === 'tokens') sorted.sort(compareByTokens);
  else if (sortBy === 'title') sorted.sort(compareByTitle);
  else if (sortBy === 'tool') sorted.sort(compareByTool);
  else sorted.sort(compareByDate);
  // Default comparators sort desc (most/newest first). Reverse for asc.
  if (sortDir === 'asc') sorted.reverse();
  return sorted;
}

const RANGE_MS: Record<string, number> = { '24h': 86400000, '7d': 604800000 };
const DEFAULT_RANGE_MS = 2592000000;

/** Keep sessions updated within the date range ('' = all time). */
export function filterByDateRange(list: SessionSummary[], dateRange: string, now = Date.now()): SessionSummary[] {
  if (!dateRange) return list;
  const ms = RANGE_MS[dateRange] ?? DEFAULT_RANGE_MS;
  return list.filter((s) => now - new Date(s.updated_at).getTime() < ms);
}

/** The session with the newest updated_at, or null for an empty list. */
export function findMostRecent(list: SessionSummary[]): SessionSummary | null {
  if (list.length === 0) return null;
  return [...list].sort(compareByDate)[0];
}

export type GroupedSessions =
  | { mode: 'tool'; toolGroups: Array<{ label: string; sessions: SessionSummary[] }>; today: SessionSummary[]; thisWeek: SessionSummary[]; earlier: SessionSummary[] }
  | { mode: 'date'; toolGroups: Array<{ label: string; sessions: SessionSummary[] }>; today: SessionSummary[]; thisWeek: SessionSummary[]; earlier: SessionSummary[] };

/** Date buckets by default; "Sort: Tool" switches to one bucket per tool. */
export function groupSessions(list: SessionSummary[], sortBy: SortKey): GroupedSessions {
  // Tool and model sorts group under their own headings so the sort order is
  // not split up by date buckets (model order can come from the server).
  if (sortBy === 'tool' || sortBy === 'model') {
    const toolGroups = new Map<string, SessionSummary[]>();
    for (const s of list) {
      const label =
        sortBy === 'tool'
          ? fullToolName(s.source_tool)
          : isUnknownModelId(s.model_id)
            ? 'Unknown model'
            : friendlyModelName(s.model_id);
      if (!toolGroups.has(label)) toolGroups.set(label, []);
      toolGroups.get(label)!.push(s);
    }
    return {
      mode: 'tool',
      toolGroups: Array.from(toolGroups.entries()).map(([label, sessions]) => ({ label, sessions })),
      today: [],
      thisWeek: [],
      earlier: [],
    };
  }

  const today: SessionSummary[] = [];
  const thisWeek: SessionSummary[] = [];
  const earlier: SessionSummary[] = [];
  for (const s of list) {
    if (isToday(s.updated_at)) today.push(s);
    else if (isThisWeek(s.updated_at)) thisWeek.push(s);
    else earlier.push(s);
  }
  return { mode: 'date', toolGroups: [], today, thisWeek, earlier };
}

export interface SessionInsights {
  toolMix: string;
  tokensToday: number;
  peak: string | null;
}

export function computeInsights(list: SessionSummary[]): SessionInsights | null {
  if (list.length === 0) return null;
  const toolCounts: Record<string, number> = {};
  let totalTokensToday = 0;
  const hourCounts = new Array(24).fill(0);

  for (const s of list) {
    toolCounts[s.source_tool] = (toolCounts[s.source_tool] || 0) + 1;
    if (isToday(s.created_at)) {
      totalTokensToday += s.total_input_tokens + s.total_output_tokens;
    }
    const h = new Date(s.created_at).getHours();
    hourCounts[h]++;
  }

  const total = list.length;
  const sortedTools = Object.entries(toolCounts).sort((a, b) => b[1] - a[1]).slice(0, 3);
  const toolMix = sortedTools.map(([tool, count]) =>
    `${fullToolName(tool)} ${Math.round((count / total) * 100)}%`
  ).join(' / ');

  let peakHour = 0;
  let peakCount = 0;
  for (let h = 0; h < 24; h++) {
    const windowCount = hourCounts[h] + hourCounts[(h + 1) % 24];
    if (windowCount > peakCount) { peakCount = windowCount; peakHour = h; }
  }
  const fmtHour = (h: number) => {
    const ampm = h >= 12 ? 'PM' : 'AM';
    return `${h % 12 || 12} ${ampm}`;
  };
  const peak = peakCount > 0 ? `${fmtHour(peakHour)}-${fmtHour((peakHour + 2) % 24)}` : null;

  return { toolMix, tokensToday: totalTokensToday, peak };
}
