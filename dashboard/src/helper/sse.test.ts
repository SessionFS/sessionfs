import { describe, expect, it } from 'vitest';
import { SseParser, parseActionProposed } from './sse';

describe('SseParser', () => {
  it('parses a single complete frame', () => {
    const p = new SseParser();
    const events = p.push('event: text\ndata: {"delta":"hi"}\n\n');
    expect(events).toEqual([{ event: 'text', data: '{"delta":"hi"}' }]);
  });

  it('parses multiple frames in one chunk', () => {
    const p = new SseParser();
    const events = p.push(
      'event: text\ndata: {"delta":"a"}\n\n' +
        'event: text\ndata: {"delta":"b"}\n\n',
    );
    expect(events).toEqual([
      { event: 'text', data: '{"delta":"a"}' },
      { event: 'text', data: '{"delta":"b"}' },
    ]);
  });

  it('joins multi-line data fields with newlines', () => {
    const p = new SseParser();
    const events = p.push('event: text\ndata: line1\ndata: line2\n\n');
    expect(events).toEqual([{ event: 'text', data: 'line1\nline2' }]);
  });

  it('ignores comment/ping frames', () => {
    const p = new SseParser();
    const events = p.push(': ping\n\n');
    expect(events).toEqual([]);
  });

  it('ignores comments mixed into a data frame', () => {
    const p = new SseParser();
    const events = p.push(': keepalive\nevent: done\ndata: {}\n\n');
    expect(events).toEqual([{ event: 'done', data: '{}' }]);
  });

  it('reassembles frames split across chunk boundaries', () => {
    const p = new SseParser();
    const first = p.push('event: te');
    expect(first).toEqual([]);
    const second = p.push('xt\ndata: {"delta":"hel');
    expect(second).toEqual([]);
    const third = p.push('lo"}\n\n');
    expect(third).toEqual([{ event: 'text', data: '{"delta":"hello"}' }]);
  });

  it('splits a chunk that contains a partial next frame', () => {
    const p = new SseParser();
    const events = p.push('event: text\ndata: {"delta":"a"}\n\nevent: to');
    expect(events).toEqual([{ event: 'text', data: '{"delta":"a"}' }]);
    expect(p.pending).toBe('event: to');
  });

  it('normalizes CRLF line endings', () => {
    const p = new SseParser();
    const events = p.push('event: text\r\ndata: {"delta":"x"}\r\n\r\n');
    expect(events).toEqual([{ event: 'text', data: '{"delta":"x"}' }]);
  });

  it('handles a CRLF pair split across chunks', () => {
    const p = new SseParser();
    expect(p.push('event: text\r')).toEqual([]);
    expect(p.push('\ndata: {"delta":"y"}\r\n\r\n')).toEqual([
      { event: 'text', data: '{"delta":"y"}' },
    ]);
  });

  it('drops a leading space after the field colon only', () => {
    const p = new SseParser();
    const events = p.push('event: text\ndata:  {"delta":"z"}\n\n');
    // One leading space stripped; the payload keeps its own spacing.
    expect(events).toEqual([{ event: 'text', data: ' {"delta":"z"}' }]);
  });

  it('exposes the pending tail when no trailing blank line is present', () => {
    const p = new SseParser();
    p.push('event: text\ndata: partial');
    expect(p.pending).toBe('event: text\ndata: partial');
  });

  it('flushes a final frame that has no trailing blank line', () => {
    const p = new SseParser();
    expect(p.push('event: text\ndata: {"delta":"hi"}')).toEqual([]);
    expect(p.flush()).toEqual([{ event: 'text', data: '{"delta":"hi"}' }]);
    expect(p.flush()).toEqual([]);
  });

  it('flushes nothing when the stream ended on a frame boundary', () => {
    const p = new SseParser();
    p.push('event: done\ndata: {}\n\n');
    expect(p.flush()).toEqual([]);
  });
});

describe('parseActionProposed', () => {
  const frame = (action: unknown) => {
    const [ev] = new SseParser().push(`event: action_proposed\ndata: ${JSON.stringify({ action })}\n\n`);
    return ev;
  };

  it('parses an action_proposed event into a proposal', () => {
    const ev = frame({
      id: 'ha_1',
      kind: 'trash_sessions',
      summary: 'Move 1 session(s) to Trash',
      targets: [
        { id: 'ses_a', name: 'A', change: 'will_change', before: 'active', after: 'trashed' },
        { id: 'ses_b', name: 'B', change: 'skipped', reason: 'not_owned' },
      ],
      params: { session_ids: ['ses_a', 'ses_b'] },
      expires_at: '2026-10-25T12:00:00+00:00',
    });
    expect(ev.event).toBe('action_proposed');
    expect(parseActionProposed(ev.data)).toEqual({
      id: 'ha_1',
      kind: 'trash_sessions',
      summary: 'Move 1 session(s) to Trash',
      targets: [
        { id: 'ses_a', name: 'A', change: 'will_change', before: 'active', after: 'trashed' },
        { id: 'ses_b', name: 'B', change: 'skipped', reason: 'not_owned' },
      ],
      params: { session_ids: ['ses_a', 'ses_b'] },
      expires_at: '2026-10-25T12:00:00+00:00',
    });
  });

  it('defaults params to an empty object when the event has none', () => {
    const ev = frame({ id: 'ha_3', kind: 'trash_sessions', targets: [], params: ['not', 'an', 'object'] });
    expect(parseActionProposed(ev.data)?.params).toEqual({});
  });

  it('drops malformed targets and rejects payloads without an id', () => {
    const ev = frame({ id: 'ha_2', kind: 'set_auto_narrative', targets: [{ id: 'p', change: 'bogus' }, null, { id: 'q', change: 'unchanged' }] });
    expect(parseActionProposed(ev.data)?.targets).toEqual([{ id: 'q', name: 'q', change: 'unchanged' }]);
    expect(parseActionProposed(ev.data)?.expires_at).toBeNull();
    expect(parseActionProposed(JSON.stringify({ action: { kind: 'x' } }))).toBeNull();
    expect(parseActionProposed('not json')).toBeNull();
  });
});
