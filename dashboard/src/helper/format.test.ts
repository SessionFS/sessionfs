import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import {
  answerAsPlainText,
  classifyError,
  describeToolStep,
  citeHref,
  historyGroup,
  codeRefTarget,
  parseRefHref,
  refHref,
  repoLinksFor,
  splitRefs,
  parseCiteHref,
  processCitations,
  relativeTime,
  sourceHref,
  type SessionSource,
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
      { kind: 'session', n: 1, sessionId: 's_b', title: 'B', messageIndexes: [2, 4], verified: true },
      { kind: 'session', n: 2, sessionId: 's_a', title: 'A', messageIndexes: [], verified: true },
    ]);
  });

  it('marks unconfirmed sessions unverified only once the turn settles', () => {
    expect((processCitations('x [session:s_x]', [], { streaming: true }).sources[0] as SessionSource).verified).toBe(true);
    expect((processCitations('x [session:s_x]', [], { settled: true }).sources[0] as SessionSource).verified).toBe(false);
  });

  it('appends cited sessions the text never marked', () => {
    const { sources } = processCitations('no markers', [{ session_id: 's_1', message_index: 7, title: 'T' }]);
    expect(sources).toEqual([{ kind: 'session', n: 1, sessionId: 's_1', title: 'T', messageIndexes: [7], verified: true }]);
  });

  it('numbers doc and session sources in one shared order', () => {
    const { markdown, sources } = processCitations(
      'See [doc:integrations/jira#setup] then [session:s_a] and [doc:quickstart].',
      [
        { slug: 'integrations/jira', anchor: 'setup', title: 'Jira Cloud', url: 'https://sessionfs.dev/integrations/jira/#setup' },
        { slug: 'quickstart', anchor: null, title: 'Quickstart', url: 'https://sessionfs.dev/quickstart/' },
        { session_id: 's_a', message_index: null, title: 'A' },
      ],
      { settled: true },
    );
    expect(markdown).toBe('See [1](#sfs-cite-1) then [2](#sfs-cite-2) and [3](#sfs-cite-3).');
    expect(sources).toEqual([
      { kind: 'doc', n: 1, slug: 'integrations/jira', anchor: 'setup', title: 'Jira Cloud', url: 'https://sessionfs.dev/integrations/jira/#setup', verified: true },
      { kind: 'session', n: 2, sessionId: 's_a', title: 'A', messageIndexes: [], verified: true },
      { kind: 'doc', n: 3, slug: 'quickstart', anchor: null, title: 'Quickstart', url: 'https://sessionfs.dev/quickstart/', verified: true },
    ]);
  });

  it('never builds a doc url client-side: no link until the server confirms it', () => {
    const { sources } = processCitations('See [doc:quickstart].', [], { streaming: true });
    expect(sources).toEqual([
      { kind: 'doc', n: 1, slug: 'quickstart', anchor: null, title: 'quickstart', url: null, verified: true },
    ]);
  });

  it('marks an unconfirmed doc unverified, with no url, once the turn settles', () => {
    const { markdown, sources } = processCitations('[doc:nope]', [], { settled: true });
    expect(sources).toEqual([
      { kind: 'doc', n: 1, slug: 'nope', anchor: null, title: 'nope', url: null, verified: false },
    ]);
    // The footnote placeholder renders as an inert chip (see HelperMessage).
    expect(markdown).toBe('[1](#sfs-cite-1)');
    expect(markdown).not.toContain('sessionfs.dev');
  });

  it('keeps the text after an unclosed doc marker, including later citations', () => {
    const text = 'Run it [doc:quickstart and see\nthe next paragraph [session:ses_1 msg:2] here.';
    for (const opts of [{ streaming: true }, { settled: true }]) {
      const { markdown, sources } = processCitations(text, [], opts);
      expect(markdown).toBe('Run it [doc:quickstart and see\nthe next paragraph [1](#sfs-cite-1-m2) here.');
      expect(sources).toHaveLength(1);
      expect(sources[0].kind).toBe('session');
    }
  });

  it('only treats the docs index charset as a doc marker', () => {
    expect(processCitations('[doc:Quick Start]', [], { settled: true }).markdown).toBe('[doc:Quick Start]');
    expect(processCitations('[doc:cli#commands--x]', [], { settled: true }).sources[0]).toMatchObject({
      slug: 'cli',
      anchor: 'commands--x',
    });
  });

  it('hides a marker still arriving at the end of a stream', () => {
    for (const tail of ['[', '[sess', '[session:', '[session:ses_ab', '[session:ses_ab msg', '[session:ses_ab msg:1']) {
      expect(processCitations(`Done ${tail}`, [], { streaming: true }).markdown).toBe('Done ');
    }
    for (const tail of ['[doc', '[doc:', '[doc:integ', '[doc:integrations/jira', '[doc:integrations/jira#set']) {
      expect(processCitations(`Done ${tail}`, [], { streaming: true }).markdown).toBe('Done ');
    }
    // Not a marker, or not streaming: kept as is.
    expect(processCitations('see [docs', [], { streaming: true }).markdown).toBe('see [docs');
    expect(processCitations('Done [session:x', [], {}).markdown).toBe('Done [session:x');
    expect(processCitations('Done [doc:integrations/jira#setup', [], {}).markdown).toBe('Done [doc:integrations/jira#setup');
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

  it('lists doc sources by their public url', () => {
    expect(
      answerAsPlainText('See [doc:quickstart].', [
        { slug: 'quickstart', anchor: null, title: 'Quickstart', url: 'https://sessionfs.dev/quickstart/' },
      ]),
    ).toBe('See [1].\n\nSources:\n[1] Quickstart (https://sessionfs.dev/quickstart/)');
  });

  it('drops doc markers the server never confirmed', () => {
    expect(answerAsPlainText('See [doc:nope].', [])).toBe('See .');
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

describe('references', () => {
  const TK = 'tk_008676e806e94bc5';
  const SES = 'ses_1a2b3c4d5e6f7a8b';
  const repo = { provider: 'github' as const, path: 'acme/widgets' };

  it('splits text at ids, PRs and SHAs', () => {
    expect(splitRefs(`See ${TK} now`, null)).toEqual(['See ', { text: TK, url: `#sfs-ref-ticket-${TK}` }, ' now']);
    expect(splitRefs('PR #394 at 839c3a7', null)).toEqual(['PR #394 at 839c3a7']);
    expect(splitRefs('PR #394 at 839c3a7', repo)).toEqual([
      { text: 'PR #394', url: 'https://github.com/acme/widgets/pull/394' },
      ' at ',
      { text: '839c3a7', url: 'https://github.com/acme/widgets/commit/839c3a7' },
    ]);
  });

  it('holds back a reference that ends the text when asked', () => {
    expect(splitRefs(`Open ${TK}`, null, true)).toEqual([`Open ${TK}`]);
    expect(splitRefs(`Open ${TK}.`, null, true)).toEqual(['Open ', { text: TK, url: `#sfs-ref-ticket-${TK}` }, '.']);
  });

  it('round-trips ref hrefs and rejects malformed ones', () => {
    expect(parseRefHref(refHref('ticket', TK))).toEqual({ kind: 'ticket', id: TK });
    expect(parseRefHref(refHref('session', SES))).toEqual({ kind: 'session', id: SES });
    expect(parseRefHref('#sfs-ref-ticket-tk_nothex')).toBeNull();
    expect(parseRefHref(`#sfs-ref-wiki-${TK}`)).toBeNull();
    expect(parseRefHref('#sfs-cite-1')).toBeNull();
  });

  it('never resolves inherited object keys as a kind', () => {
    for (const kind of ['constructor', 'toString', '__proto__', 'valueOf', 'hasOwnProperty', 'isPrototypeOf']) {
      expect(() => parseRefHref(`#sfs-ref-${kind}-a`)).not.toThrow();
      expect(parseRefHref(`#sfs-ref-${kind}-a`)).toBeNull();
      expect(parseRefHref(`#sfs-ref-${kind}-${TK}`)).toBeNull();
    }
  });

  it('links a code span only when it is exactly one reference', () => {
    expect(codeRefTarget(` ${TK} `, null)).toBe(`#sfs-ref-ticket-${TK}`);
    expect(codeRefTarget(`sfs ticket show ${TK}`, null)).toBeNull();
    expect(codeRefTarget('839c3a7f', null)).toBeNull();
    expect(codeRefTarget('839c3a7f', repo)).toBe('https://github.com/acme/widgets/commit/839c3a7f');
  });

  it('derives GitHub links only from one verified GitHub repo', () => {
    expect(repoLinksFor([{ git_remote_normalized: 'acme/widgets', provider: 'github' }])).toEqual(repo);
    expect(repoLinksFor([{ git_remote_normalized: 'acme/widgets', provider: null }])).toBeNull();
    expect(repoLinksFor(undefined)).toBeNull();
    expect(
      repoLinksFor([
        { git_remote_normalized: 'acme/a', provider: 'github' },
        { git_remote_normalized: 'acme/b', provider: 'github' },
      ]),
    ).toBeNull();
    expect(repoLinksFor([{ git_remote_normalized: 'grp/sub/repo', provider: 'github' }])).toBeNull();
  });
});
