/**
 * Server-Sent Events parsing for the session helper stream.
 *
 * The dashboard streams answers with `fetch` + `ReadableStream` (EventSource
 * cannot attach an `Authorization` header), so it has to turn the raw byte
 * chunks into structured events itself. This is the one place that does that:
 * feed it chunks as they arrive and it yields complete events, correctly
 * handling frames split across chunk boundaries, `\r\n` line endings, `data:`
 * lines that continue over several lines, and comment/ping lines (`: ping`).
 *
 * The helper endpoint emits frames of the shape `event: <type>\ndata: <json>`.
 */

import type { HelperActionProposal, HelperActionTarget } from '../api/client';

export interface SseEvent {
  /** The `event:` field — the event type (e.g. `text`, `tool_result`, `done`). */
  event: string;
  /** The joined `data:` fields of the frame (multi-line data joined by `\n`). */
  data: string;
}

/**
 * Parse one complete frame (the lines before a blank line) into an event.
 * Returns null for frames that carry no data — comment/ping frames.
 */
function parseFrame(raw: string): SseEvent | null {
  const lines = raw.split('\n');
  let event = '';
  const dataLines: string[] = [];
  let sawData = false;

  for (const line of lines) {
    // Blank lines and comments (`: ping`) are ignored.
    if (line === '' || line.startsWith(':')) continue;

    const colon = line.indexOf(':');
    if (colon === -1) {
      // A field with no colon and no value (e.g. a bare `data` line).
      if (line === 'data') {
        dataLines.push('');
        sawData = true;
      }
      continue;
    }

    const field = line.slice(0, colon);
    let value = line.slice(colon + 1);
    // A single leading space after the colon is part of the framing, not the
    // value, per the SSE spec.
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'event') {
      event = value;
    } else if (field === 'data') {
      dataLines.push(value);
      sawData = true;
    }
    // `id:` / `retry:` are irrelevant to this stream and ignored.
  }

  if (!sawData) return null;
  return { event, data: dataLines.join('\n') };
}

/**
 * Incremental SSE parser. Append chunks of the stream body as they arrive and
 * collect the complete events each append produces. Keeps the tail of a frame
 * (if a frame straddles a chunk boundary) buffered until the rest arrives.
 */
export class SseParser {
  private buffer = '';

  push(chunk: string): SseEvent[] {
    this.buffer += chunk;
    // Normalize CRLF to LF so frame splitting and line parsing below see one
    // convention. Doing this on the accumulated buffer (not the chunk) also
    // covers a `\r\n` pair split across chunks: the lone `\r` stays buffered
    // until the next chunk's `\n` arrives and completes the pair. Bare `\r`
    // line endings don't occur in this stream (the server frames with `\n`).
    this.buffer = this.buffer.replace(/\r\n/g, '\n');

    const events: SseEvent[] = [];
    let idx: number;
    // A frame ends at a blank line; find each complete frame and drain it.
    while ((idx = this.buffer.indexOf('\n\n')) !== -1) {
      const frame = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 2);
      const event = parseFrame(frame);
      if (event) events.push(event);
    }
    return events;
  }

  /** Emit the final frame when the stream ends without a trailing blank line.
   *  Call after the last `push` to drain a complete frame that was never
   *  terminated by `\n\n` (e.g. `event: done\ndata: {…}` at end of stream). */
  flush(): SseEvent[] {
    if (!this.buffer) return [];
    const frame = this.buffer;
    this.buffer = '';
    const event = parseFrame(frame);
    return event ? [event] : [];
  }

  /** Any partial frame still buffered (empty between frames). */
  get pending(): string {
    return this.buffer;
  }
}

const ACTION_CHANGES = new Set(['will_change', 'unchanged', 'skipped']);

/**
 * Parse the data of an `action_proposed` event (`{action: {...}}`) into a
 * proposal the drawer can render as a confirm card. Returns null when the
 * payload is malformed; targets that don't match the shape are dropped.
 */
export function parseActionProposed(data: string): HelperActionProposal | null {
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  const a = (raw as { action?: unknown } | null)?.action as Record<string, unknown> | undefined;
  if (!a || typeof a !== 'object' || typeof a.id !== 'string' || !a.id || typeof a.kind !== 'string') {
    return null;
  }
  const targets: HelperActionTarget[] = [];
  for (const t of Array.isArray(a.targets) ? a.targets : []) {
    const r = t as Record<string, unknown> | null;
    if (!r || typeof r.id !== 'string' || typeof r.change !== 'string' || !ACTION_CHANGES.has(r.change)) continue;
    targets.push({ ...(r as object), id: r.id, name: typeof r.name === 'string' ? r.name : r.id } as HelperActionTarget);
  }
  return {
    id: a.id,
    kind: a.kind,
    summary: typeof a.summary === 'string' ? a.summary : '',
    targets,
    expires_at: typeof a.expires_at === 'string' ? a.expires_at : null,
  };
}
