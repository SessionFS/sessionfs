import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { answerAsPlainText, citeHref, processCitations, type MeetingSource } from './format';
import { CitationChip } from './HelperMessage';

/** Chat citations in helper answers: `[chat:<id> M<a>-<b>]` and `chat_citation` events (GitHub #118). */

const ID = 'chat_0123456789abcdef';
const CITATION = { kind: 'chat' as const, meeting_id: ID, title: 'LB cert', channel_name: '#infra', line_start: 12, line_end: 15 };

describe('chat citation markers', () => {
  it('number the thread and keep the cited message range', () => {
    const { markdown, sources } = processCitations(
      `We chose Dataproc [chat:${ID} M12-15] and Sam owns it [chat:${ID} M20].`,
      [CITATION],
      { settled: true },
    );
    expect(markdown).toBe(
      `We chose Dataproc [1](${citeHref(1, undefined, { start: 12, end: 15 })}) and Sam owns it [1](${citeHref(1, undefined, { start: 20, end: 20 })}).`,
    );
    const s = sources[0] as MeetingSource;
    expect(s).toMatchObject({ kind: 'meeting', chat: true, meetingId: ID, channelName: '#infra', verified: true });
    expect(s.ranges).toEqual([
      { start: 12, end: 15 },
      { start: 20, end: 20 },
    ]);
  });

  it('mark an unconfirmed chat marker unverified once settled', () => {
    const { sources } = processCitations(`x [chat:${ID} M1-2]`, [], { settled: true });
    expect((sources[0] as MeetingSource).verified).toBe(false);
  });

  it('do not treat a meeting marker with a chat id as chat-less', () => {
    const { sources } = processCitations(`x [meeting:meet_0123456789abcdef L1-2]`, [], { settled: true });
    expect((sources[0] as MeetingSource).chat).toBe(false);
  });

  it('hide a chat marker still streaming in', () => {
    expect(processCitations('Owner [chat:chat_0123 M1', [], { streaming: true }).markdown).toBe('Owner ');
    expect(processCitations('Owner [chat:', [], { streaming: true }).markdown).toBe('Owner ');
  });

  it('list chat sources in the plain-text copy with M ranges', () => {
    const text = answerAsPlainText(`Done [chat:${ID} M12-15].`, [CITATION]);
    expect(text).toContain(`Done [1].`);
    expect(text).toContain(`[1] LB cert (chat ${ID} M12-15)`);
  });
});

describe('chat citation chip', () => {
  it('links to the conversation at the message range', () => {
    const { sources } = processCitations(`x [chat:${ID} M12-15]`, [CITATION], { settled: true });
    render(
      <MemoryRouter>
        <CitationChip source={sources[0]} lines={{ start: 12, end: 15 }} onNavigate={vi.fn()} />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', `/meetings/${ID}?lines=12-15`);
    expect(link).toHaveAccessibleName(/LB cert \(#infra\), messages 12–15/);
  });
});
