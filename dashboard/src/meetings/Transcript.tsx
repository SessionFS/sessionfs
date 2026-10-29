import { memo, useEffect, useMemo, useRef } from 'react';
import { splitSpeaker, transcriptLines, type LineRange } from './format';

/**
 * The stored transcript with 1-based line numbers. Line numbers are the ones
 * action items and helper citations refer to; `highlight` tints that range
 * and scrolls it into view whenever `scrollKey` changes (an item click or a
 * citation link), so re-clicking the same item scrolls back to it.
 */
interface TranscriptProps {
  content: string;
  highlight: LineRange | null;
  /** Changes every time the highlight should be scrolled to. */
  scrollKey?: number;
  className?: string;
}

export function Transcript({ content, highlight, scrollKey = 0, className = '' }: TranscriptProps) {
  const lines = useMemo(() => transcriptLines(content), [content]);
  const listRef = useRef<HTMLOListElement>(null);

  const start = highlight?.start;
  useEffect(() => {
    if (start == null) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-line="${start}"]`);
    // jsdom has no scrollIntoView; real browsers scroll the panel and page.
    el?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
  }, [start, scrollKey]);

  if (lines.length === 0) {
    return <p className="px-4 py-6 text-sm text-text-tertiary">This transcript is empty.</p>;
  }

  return (
    <ol
      ref={listRef}
      aria-label="Transcript"
      className={`py-2 text-sm leading-6 ${className}`}
    >
      {lines.map((line, i) => {
        const n = i + 1;
        const on = !!highlight && n >= highlight.start && n <= highlight.end;
        return <TranscriptLine key={n} n={n} text={line} on={on} edge={!on ? null : n === highlight!.start ? 'start' : n === highlight!.end ? 'end' : null} />;
      })}
    </ol>
  );
}

const TranscriptLine = memo(function TranscriptLine({
  n,
  text,
  on,
  edge,
}: {
  n: number;
  text: string;
  on: boolean;
  edge: 'start' | 'end' | null;
}) {
  const speaker = splitSpeaker(text);
  return (
    <li
      data-line={n}
      data-highlighted={on || undefined}
      aria-current={on && edge === 'start' ? 'location' : undefined}
      className={`grid grid-cols-[3rem_minmax(0,1fr)] gap-3 pr-4 scroll-mt-24 border-l-2 transition-colors ${
        on
          ? 'border-brand bg-[color-mix(in_oklch,var(--brand)_10%,transparent)]'
          : 'border-transparent'
      }`}
    >
      <span
        className={`select-none text-right font-mono text-2xs leading-6 tabular-nums ${
          on ? 'text-brand font-semibold' : 'text-text-tertiary'
        }`}
      >
        <span className="sr-only">Line </span>
        {n}
      </span>
      <span className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere] text-text-primary">
        {speaker ? (
          <>
            <span className="font-medium text-text-secondary">{speaker[0]}:</span> {speaker[1]}
          </>
        ) : (
          text || ' '
        )}
      </span>
    </li>
  );
});
