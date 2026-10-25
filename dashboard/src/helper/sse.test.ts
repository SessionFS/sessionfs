import { describe, expect, it } from 'vitest';
import { SseParser } from './sse';

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
