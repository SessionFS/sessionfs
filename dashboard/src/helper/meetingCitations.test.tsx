import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { answerAsPlainText, citeHref, parseCiteHref, processCitations, type MeetingSource } from './format';
import { CitationChip } from './HelperMessage';

/** Meeting citations in helper answers: `[meeting:<id> L<a>-<b>]` (GitHub #116). */

const ID = 'meet_0123456789abcdef';

describe('meeting citation markers', () => {
  it('number the meeting and keep the cited range on the inline link', () => {
    const { markdown, sources } = processCitations(
      `Sam owns the rollout plan [meeting:${ID} L3-4] and Kim the copy [meeting:${ID} L5].`,
      [{ kind: 'meeting', meeting_id: ID, title: 'Weekly sync', line_start: 3, line_end: 4 }],
      { settled: true },
    );
    expect(markdown).toBe(
      `Sam owns the rollout plan [1](${citeHref(1, undefined, { start: 3, end: 4 })}) and Kim the copy [1](${citeHref(1, undefined, { start: 5, end: 5 })}).`,
    );
    expect(sources).toHaveLength(1);
    const s = sources[0] as MeetingSource;
    expect(s).toMatchObject({ kind: 'meeting', meetingId: ID, title: 'Weekly sync', verified: true });
    expect(s.ranges).toEqual([
      { start: 3, end: 4 },
      { start: 5, end: 5 },
    ]);
  });

  it('share footnote numbers with other kinds and mark unconfirmed meetings', () => {
    const { sources } = processCitations(
      `See [session:ses_abcdefgh12 msg:2] and [meeting:${ID} L1-2].`,
      [{ session_id: 'ses_abcdefgh12', message_index: 2, title: 'S' }],
      { settled: true },
    );
    expect(sources.map((s) => [s.kind, s.n])).toEqual([
      ['session', 1],
      ['meeting', 2],
    ]);
    expect((sources[1] as MeetingSource).verified).toBe(false);
  });

  it('hide a marker still streaming in', () => {
    const { markdown } = processCitations(`Owner is Sam [meeting:meet_0123 L1`, [], { streaming: true });
    expect(markdown).toBe('Owner is Sam ');
  });

  it('round-trip the cited range through the href', () => {
    expect(parseCiteHref(citeHref(4, undefined, { start: 10, end: 12 }))).toEqual({
      n: 4,
      messageIndex: undefined,
      lines: { start: 10, end: 12 },
    });
    expect(parseCiteHref(citeHref(2, 7))).toEqual({ n: 2, messageIndex: 7, lines: undefined });
  });

  it('list meeting sources in the plain-text copy', () => {
    const text = answerAsPlainText(`Plan [meeting:${ID} L3-4].`, [
      { kind: 'meeting', meeting_id: ID, title: 'Weekly sync', line_start: 3, line_end: 4 },
    ]);
    expect(text).toBe(`Plan [1].\n\nSources:\n[1] Weekly sync (meeting ${ID} L3-4)`);
  });
});

describe('meeting citation chip', () => {
  const source: MeetingSource = {
    kind: 'meeting',
    n: 1,
    meetingId: ID,
    title: 'Weekly sync',
    ranges: [{ start: 3, end: 4 }],
    verified: true,
    at: null,
  };

  it('opens the meeting at the marker’s lines', () => {
    render(
      <MemoryRouter>
        <CitationChip source={source} lines={{ start: 5, end: 5 }} onNavigate={vi.fn()} />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link', { name: 'Source 1: Weekly sync, line 5' });
    expect(link).toHaveAttribute('href', `/meetings/${ID}?lines=5`);
  });

  it('falls back to the first cited range, and is inert when unconfirmed', () => {
    const { rerender } = render(
      <MemoryRouter>
        <CitationChip source={source} onNavigate={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'Source 1: Weekly sync, lines 3–4' })).toHaveAttribute(
      'href',
      `/meetings/${ID}?lines=3-4`,
    );
    rerender(
      <MemoryRouter>
        <CitationChip source={{ ...source, verified: false }} onNavigate={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByTitle('Source not available to you')).toHaveTextContent('1');
  });
});
