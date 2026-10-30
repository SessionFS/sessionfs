import type { Harm } from '../../api/reviewLoop';

export const HARM_LABEL: Record<Harm, string> = {
  'data-exposed': 'Data exposed',
  'money-spent': 'Money spent',
  'wrong-actor': 'Wrong person',
  crash: 'Crash',
  other: 'Other',
  none: 'No harm',
};

/** Only these count as harm (and only these use the loud colour). */
export function isHarmful(harm: Harm | string | undefined | null): boolean {
  return harm === 'data-exposed' || harm === 'money-spent' || harm === 'wrong-actor' || harm === 'crash';
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k`;
  return String(n);
}

export function formatUsd(n: number): string {
  return `$${n.toFixed(n >= 100 ? 0 : 2)}`;
}

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 7) : '';
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function projectLoopHref(projectId: string, extra: Record<string, string> = {}): string {
  const sp = new URLSearchParams({ tab: 'work', sub: 'loop', ...extra });
  return `/projects/${encodeURIComponent(projectId)}?${sp.toString()}`;
}
export const storyHref = (projectId: string, ticketId: string) => projectLoopHref(projectId, { story: ticketId });
export const runHref = (projectId: string, runId: string) =>
  `/projects/${encodeURIComponent(projectId)}?${new URLSearchParams({ tab: 'work', sub: 'agent-runs', run: runId })}`;
export const ticketHref = (projectId: string, ticketId: string) =>
  `/projects/${encodeURIComponent(projectId)}?${new URLSearchParams({ tab: 'work', sub: 'tickets', ticket: ticketId })}`;

export const DOCS_URL = 'https://sessionfs.dev/review-loop/';
