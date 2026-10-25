import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import {
  answerAsPlainText,
  classifyError,
  describeToolStep,
  citeHref,
  historyGroup,
  parseCiteHref,
  processCitations,
  relativeTime,
  sourceHref,
} from './format';
import { activeOption, scopeOptions } from './scope';

describe('processCitations', () => {
  it('numbers sessions by first appearance and merges message indexes', () => {
    const { markdown, sources } = processCitations(
      'A [session:s_b msg:4] then [session:s_a] and [session:s_b msg:2].',
      [
        { session_id: 's_b', message_index: 2, title: 'B' },
        { session_id: 's_b', message_index: 4, title: 'B' },
        { session_id: 's_a', message_index: null, title: 'A' },
      ],
      { settled: true },
    );
    expect(markdown).toBe('A [1](#sfs-cite-1-m4) then [2](#sfs-cite-2) and [1](#sfs-cite-1-m2).');
    expect(sources).toEqual([
      { n: 1, sessionId: 's_b', title: 'B', messageIndexes: [2, 4], verified: true },
      { n: 2, sessionId: 's_a', title: 'A', messageIndexes: [], verified: true },
    ]);
  });

  it('marks unconfirmed sessions unverified only once the turn settles', () => {
    expect(processCitations('x [session:s_x]', [], { streaming: true }).sources[0].verified).toBe(true);
    expect(processCitations('x [session:s_x]', [], { settled: true }).sources[0].verified).toBe(false);
  });

  it('appends cited sessions the text never marked', () => {
    const { sources } = processCitations('no markers', [{ session_id: 's_1', message_index: 7, title: 'T' }]);
    expect(sources).toEqual([{ n: 1, sessionId: 's_1', title: 'T', messageIndexes: [7], verified: true }]);
  });

  it('hides a marker still arriving at the end of a stream', () => {
    for (const tail of ['[', '[sess', '[session:', '[session:ses_ab', '[session:ses_ab msg', '[session:ses_ab msg:1']) {
      expect(processCitations(`Done ${tail}`, [], { streaming: true }).markdown).toBe('Done ');
    }
    // Not a marker, or not streaming: kept as is.
    expect(processCitations('see [docs', [], { streaming: true }).markdown).toBe('see [docs');
    expect(processCitations('Done [session:x', [], {}).markdown).toBe('Done [session:x');
  });
});

describe('citation links', () => {
  it('round-trips footnote hrefs with and without a message', () => {
    expect(citeHref(3)).toBe('#sfs-cite-3');
    expect(citeHref(3, 0)).toBe('#sfs-cite-3-m0');
    expect(parseCiteHref('#sfs-cite-3')).toEqual({ n: 3, messageIndex: undefined });
    expect(parseCiteHref('#sfs-cite-3-m42')).toEqual({ n: 3, messageIndex: 42 });
    expect(parseCiteHref('#sfs-cite-x')).toBeNull();
    expect(parseCiteHref('#sfs-cite-1-m')).toBeNull();
    expect(parseCiteHref('/sessions/a')).toBeNull();
  });

  it('opens the session at the cited message and encodes the id', () => {
    expect(sourceHref('ses_1')).toBe('/sessions/ses_1');
    expect(sourceHref('ses_1', null)).toBe('/sessions/ses_1');
    expect(sourceHref('ses_1', 0)).toBe('/sessions/ses_1?msg=0');
    expect(sourceHref('a/b?c', 7)).toBe('/sessions/a%2Fb%3Fc?msg=7');
  });
});

describe('answerAsPlainText', () => {
  it('swaps markers for numbers and lists the sources', () => {
    expect(answerAsPlainText('Yes [session:s_1].', [{ session_id: 's_1', message_index: null, title: 'Login' }])).toBe(
      'Yes [1].\n\nSources:\n[1] Login (session s_1)',
    );
    expect(answerAsPlainText('No sources.', [])).toBe('No sources.');
  });
});

describe('describeToolStep', () => {
  it('reads as a sentence in the running and finished tense', () => {
    expect(describeToolStep({ name: 'search_sessions', args: { query: 'auth' }, status: 'running' })).toBe(
      'Searching sessions for “auth”',
    );
    expect(describeToolStep({ name: 'search_sessions', args: { query: 'auth' }, status: 'ok' })).toBe(
      'Searched sessions for “auth”',
    );
    expect(describeToolStep({ name: 'summarize_sessions', args: { session_ids: ['a', 'b'] }, status: 'ok' })).toBe(
      'Summarized 2 sessions',
    );
    expect(describeToolStep({ name: 'sessions_touching_path', args: { path: 'src/a.py' }, status: 'ok' })).toBe(
      'Found sessions that touched “src/a.py”',
    );
    expect(describeToolStep({ name: 'brand_new_tool', args: {}, status: 'ok' })).toBe('Ran brand new tool');
  });

  it('clips long arguments', () => {
    const label = describeToolStep({ name: 'search_sessions', args: { query: 'x'.repeat(80) }, status: 'ok' });
    expect(label.length).toBeLessThan(80);
    expect(label.endsWith('…”')).toBe(true);
  });
});

describe('classifyError', () => {
  it('reads codes from both error envelope shapes', () => {
    expect(classifyError(new ApiError(409, '{"detail":{"code":"helper_busy","message":"m"}}')).kind).toBe('busy');
    expect(classifyError(new ApiError(409, '{"error":{"code":"scope_mismatch","message":"m"}}')).kind).toBe(
      'scope_mismatch',
    );
    expect(classifyError(new ApiError(409, '{"detail":{"code":"helper_key_required","message":"m"}}')).kind).toBe(
      'key_required',
    );
    expect(classifyError(new ApiError(403, '{"detail":{"code":"upgrade_required"}}')).kind).toBe('upgrade_required');
    expect(classifyError(new ApiError(502, 'bad gateway')).kind).toBe('provider');
    expect(classifyError(new TypeError('Failed to fetch')).kind).toBe('network');
    expect(classifyError(new Error('weird')).message).toBe('weird');
  });
});

describe('time helpers', () => {
  const now = Date.parse('2026-10-25T12:00:00');
  it('formats relative times', () => {
    expect(relativeTime('2026-10-25T11:59:40', now)).toBe('just now');
    expect(relativeTime('2026-10-25T11:48:00', now)).toBe('12 min ago');
    expect(relativeTime('2026-10-25T09:00:00', now)).toBe('3 h ago');
    expect(relativeTime('2026-10-24T10:00:00', now)).toBe('yesterday');
    expect(relativeTime('not a date', now)).toBe('');
  });
  it('groups history by day', () => {
    expect(historyGroup('2026-10-25T01:00:00', now)).toBe('Today');
    expect(historyGroup('2026-10-24T23:00:00', now)).toBe('Yesterday');
    expect(historyGroup('2026-10-20T10:00:00', now)).toBe('Previous 7 days');
    expect(historyGroup('2026-09-01T10:00:00', now)).toBe('Older');
  });
});

describe('scope options', () => {
  it('offers the entry scope, the thread scope and all sessions, labelled', () => {
    const entry = { project_id: 'p1', label: 'Acme' };
    expect(scopeOptions(entry, entry).map((o) => o.label)).toEqual(['Acme', 'All my sessions']);
    expect(scopeOptions(undefined, { session_ids: ['a', 'b'] }).map((o) => o.label)).toEqual([
      '2 sessions',
      'All my sessions',
    ]);
    // A restored thread's scope borrows the entry's label when it matches.
    expect(activeOption(entry, { project_id: 'p1' }).label).toBe('Acme');
    expect(activeOption(entry, { project_id: 'p2' }).label).toBe('This project');
    expect(activeOption(entry, undefined).label).toBe('All my sessions');
  });
});
