import { memo, useEffect, useMemo, useRef } from 'react';
import type { MeetingDetail } from '../api/client';
import { chatMessages, messageTimeLabel, type ChatMessage } from './chat';
import type { LineRange } from './format';

interface ChatMessagesProps {
  thread: Pick<MeetingDetail, 'content' | 'messages'>;
  highlight: LineRange | null;
  /** Changes every time the highlight should be scrolled to. */
  scrollKey?: number;
}

/**
 * A chat thread as numbered messages (M1, M2, …): the numbers are the ones
 * action items and helper citations refer to. Message text is untrusted data
 * and is always rendered as plain text, never as markdown or HTML.
 */
export function ChatMessages({ thread, highlight, scrollKey = 0 }: ChatMessagesProps) {
  const messages = useMemo(() => chatMessages(thread), [thread]);
  const listRef = useRef<HTMLOListElement>(null);
  const start = highlight?.start;
  useEffect(() => {
    if (start == null) return;
    listRef.current?.querySelector<HTMLElement>(`[data-line="${start}"]`)?.scrollIntoView?.({
      block: 'center',
      behavior: 'smooth',
    });
  }, [start, scrollKey]);

  if (messages.length === 0) {
    return <p className="px-4 py-6 text-sm text-text-tertiary">This thread is empty.</p>;
  }
  return (
    <ol ref={listRef} aria-label="Messages" className="py-2 text-sm leading-6">
      {messages.map((m) => {
        const on = !!highlight && m.n >= highlight.start && m.n <= highlight.end;
        return <MessageRow key={m.n} message={m} on={on} first={on && m.n === highlight!.start} />;
      })}
    </ol>
  );
}

const MessageRow = memo(function MessageRow({
  message: m,
  on,
  first,
}: {
  message: ChatMessage;
  on: boolean;
  first: boolean;
}) {
  const time = messageTimeLabel(m.postedAt);
  return (
    <li
      data-line={m.n}
      data-highlighted={on || undefined}
      aria-current={first ? 'location' : undefined}
      className={`grid grid-cols-[3rem_minmax(0,1fr)] gap-3 pr-4 scroll-mt-24 border-l-2 transition-colors ${
        on ? 'border-brand bg-[color-mix(in_oklch,var(--brand)_10%,transparent)]' : 'border-transparent'
      }`}
    >
      <span
        className={`select-none text-right font-mono text-2xs leading-6 tabular-nums ${
          on ? 'text-brand font-semibold' : 'text-text-tertiary'
        }`}
      >
        <span className="sr-only">Message </span>M{m.n}
      </span>
      <span className="min-w-0 py-0.5">
        {(m.author || time) && (
          <span className="flex flex-wrap items-baseline gap-x-2 text-xs">
            {m.author && <span className="font-medium text-text-secondary [overflow-wrap:anywhere]">{m.author}</span>}
            {time && <span className="text-text-tertiary">{time}</span>}
          </span>
        )}
        <span className="block whitespace-pre-wrap [overflow-wrap:anywhere] text-text-primary">{m.text || ' '}</span>
      </span>
    </li>
  );
});
