import { describe, it, expect } from 'vitest';
import type { SessionSummary } from '../../api/client';
import {
  computeInsights, filterByDateRange, findDuplicateGroups, findMostRecent,
  groupByLineage, groupSessions, sortSessions,
} from './sessionSort';

function mk(id: string, over: Partial<SessionSummary> & Record<string, unknown> = {}): SessionSummary {
  return {
    id,
    title: id,
    source_tool: 'claude-code',
    message_count: 1,
    total_input_tokens: 0,
    total_output_tokens: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    parent_session_id: null,
    ...over,
  } as SessionSummary;
}

describe('sortSessions', () => {
  const a = mk('a', { title: 'Alpha', message_count: 5, updated_at: '2026-01-01T00:00:00Z', total_input_tokens: 10 });
  const b = mk('b', { title: 'Beta', message_count: 9, updated_at: '2026-01-03T00:00:00Z', total_output_tokens: 50 });
  const c = mk('c', { title: 'Beta', message_count: 1, updated_at: '2026-01-02T00:00:00Z', source_tool: 'codex' });

  it('sorts by date newest first and reverses for asc', () => {
    expect(sortSessions([a, b, c], 'date').map(s => s.id)).toEqual(['b', 'c', 'a']);
    expect(sortSessions([a, b, c], 'date', 'asc').map(s => s.id)).toEqual(['a', 'c', 'b']);
  });
  it('sorts by messages and tokens descending', () => {
    expect(sortSessions([a, b, c], 'messages').map(s => s.id)).toEqual(['b', 'a', 'c']);
    expect(sortSessions([a, b, c], 'tokens').map(s => s.id)).toEqual(['b', 'a', 'c']);
  });
  it('breaks title ties by date', () => {
    expect(sortSessions([a, c, b], 'title').map(s => s.id)).toEqual(['a', 'b', 'c']);
  });
  it('sorts by model name descending, then asc, with date tie-break', () => {
    const gpt = mk('gpt', { model_id: 'gpt-5-20250127', updated_at: '2026-01-01T00:00:00Z' });
    const claudeOld = mk('claudeOld', { model_id: 'claude-sonnet-4-6', updated_at: '2026-01-02T00:00:00Z' });
    const claudeNew = mk('claudeNew', { model_id: 'claude-sonnet-4-6', updated_at: '2026-01-03T00:00:00Z' });
    // desc: z → a (gpt before claude); ties break newest first
    expect(sortSessions([claudeOld, gpt, claudeNew], 'model').map(s => s.id))
      .toEqual(['gpt', 'claudeNew', 'claudeOld']);
    // asc: a → z; ties still break newest first (matches the server)
    expect(sortSessions([claudeOld, gpt, claudeNew], 'model', 'asc').map(s => s.id))
      .toEqual(['claudeNew', 'claudeOld', 'gpt']);
  });
  it('puts unknown models (null, "", "unknown", <synthetic>) last in both directions', () => {
    const gpt = mk('gpt', { model_id: 'gpt-5', updated_at: '2026-01-01T00:00:00Z' });
    const claude = mk('claude', { model_id: 'claude-sonnet-4-6', updated_at: '2026-01-01T00:00:00Z' });
    const nul = mk('nul', { model_id: null, updated_at: '2026-01-04T00:00:00Z' });
    const empty = mk('empty', { model_id: '', updated_at: '2026-01-03T00:00:00Z' });
    const literal = mk('literal', { model_id: 'unknown', updated_at: '2026-01-02T00:00:00Z' });
    const synth = mk('synth', { model_id: '<synthetic>', updated_at: '2026-01-01T12:00:00Z' });
    const input = [synth, nul, gpt, literal, claude, empty];
    expect(sortSessions(input, 'model', 'desc').map(s => s.id))
      .toEqual(['gpt', 'claude', 'nul', 'empty', 'literal', 'synth']);
    expect(sortSessions(input, 'model', 'asc').map(s => s.id))
      .toEqual(['claude', 'gpt', 'nul', 'empty', 'literal', 'synth']);
  });
  it('does not mutate the input', () => {
    const input = [a, b, c];
    sortSessions(input, 'messages');
    expect(input.map(s => s.id)).toEqual(['a', 'b', 'c']);
  });
});

describe('groupByLineage', () => {
  it('nests descendants under the root ordered oldest first', () => {
    const root = mk('r');
    const k1 = mk('k1', { parent_session_id: 'r', updated_at: '2026-01-05T00:00:00Z' });
    const k2 = mk('k2', { parent_session_id: 'k1', updated_at: '2026-01-02T00:00:00Z' });
    const other = mk('o');
    const groups = groupByLineage([root, k1, k2, other]);
    expect(groups.map(g => g.root.id)).toEqual(['r', 'o']);
    expect(groups[0].children.map(s => s.id)).toEqual(['k2', 'k1']);
  });
  it('treats a session whose parent is not in the list as a root', () => {
    const groups = groupByLineage([mk('x', { parent_session_id: 'missing' })]);
    expect(groups).toHaveLength(1);
    expect(groups[0].children).toEqual([]);
  });
  it('survives a parent cycle', () => {
    const groups = groupByLineage([
      mk('p', { parent_session_id: 'q' }),
      mk('q', { parent_session_id: 'p' }),
    ]);
    expect(Array.isArray(groups)).toBe(true);
  });
});

describe('findDuplicateGroups', () => {
  it('groups only sessions with identical title, count, tool and etag', () => {
    const d1 = mk('d1', { title: 'T', message_count: 3, etag: 'e1' });
    const d2 = mk('d2', { title: 'T', message_count: 3, etag: 'e1' });
    const diff = mk('d3', { title: 'T', message_count: 3, etag: 'e2' });
    const dupes = findDuplicateGroups([d1, d2, diff]);
    expect(dupes.size).toBe(1);
    expect([...dupes.values()][0].map(s => s.id)).toEqual(['d1', 'd2']);
  });
});

describe('filterByDateRange', () => {
  const now = Date.parse('2026-02-10T12:00:00Z');
  const fresh = mk('f', { updated_at: '2026-02-10T08:00:00Z' });
  const week = mk('w', { updated_at: '2026-02-05T12:00:00Z' });
  const old = mk('o', { updated_at: '2025-12-01T12:00:00Z' });
  it('returns everything for all time', () => {
    expect(filterByDateRange([fresh, week, old], '', now)).toHaveLength(3);
  });
  it('applies 24h, 7d and 30d windows', () => {
    expect(filterByDateRange([fresh, week, old], '24h', now).map(s => s.id)).toEqual(['f']);
    expect(filterByDateRange([fresh, week, old], '7d', now).map(s => s.id)).toEqual(['f', 'w']);
    expect(filterByDateRange([fresh, week, old], '30d', now).map(s => s.id)).toEqual(['f', 'w']);
  });
});

describe('findMostRecent / groupSessions / computeInsights', () => {
  it('finds the newest session', () => {
    expect(findMostRecent([])).toBeNull();
    const list = [mk('a', { updated_at: '2026-01-01T00:00:00Z' }), mk('b', { updated_at: '2026-01-02T00:00:00Z' })];
    expect(findMostRecent(list)?.id).toBe('b');
  });
  it('buckets by tool when sorting by tool', () => {
    const g = groupSessions([mk('a'), mk('b', { source_tool: 'codex' }), mk('c')], 'tool');
    expect(g.mode).toBe('tool');
    expect(g.toolGroups.map(t => t.sessions.length)).toEqual([2, 1]);
  });
  it('groups a model sort by model in list order, unknown models together', () => {
    const g = groupSessions(
      [mk('a', { model_id: 'gpt-5' }), mk('b', { model_id: 'gpt-5' }), mk('c', { model_id: 'claude-sonnet-4' }), mk('d', { model_id: null }), mk('e', { model_id: '<synthetic>' })],
      'model',
    );
    expect(g.mode).toBe('tool');
    expect(g.toolGroups.map((x) => [x.label, x.sessions.map((s) => s.id)])).toEqual([
      ['gpt-5', ['a', 'b']],
      ['claude-sonnet-4', ['c']],
      ['Unknown model', ['d', 'e']],
    ]);
  });

  it('buckets by date otherwise', () => {
    const now = new Date().toISOString();
    const g = groupSessions([mk('a', { updated_at: now }), mk('b', { updated_at: '2020-01-01T00:00:00Z' })], 'date');
    expect(g.mode).toBe('date');
    expect(g.today.map(s => s.id)).toEqual(['a']);
    expect(g.earlier.map(s => s.id)).toEqual(['b']);
  });
  it('computes insights or null', () => {
    expect(computeInsights([])).toBeNull();
    const ins = computeInsights([mk('a'), mk('b')]);
    expect(ins?.toolMix).toMatch(/100%/);
    expect(ins?.peak).not.toBeNull();
  });
});
