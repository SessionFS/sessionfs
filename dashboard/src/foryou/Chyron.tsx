import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ChyronEntry } from '../api/client';
import { CHYRON_LABEL } from './model';
import { Icon } from './parts';

interface Props {
  entries: ChyronEntry[];
  cleared: Set<string>;
  reduce: boolean;
  onPick: (itemKey: string) => void;
}

/**
 * "Right now": the top items as a scrolling ticker. It pauses on hover, on
 * focus and with the pause button; it scrolls a focused item into view; and
 * with reduced motion it becomes a static stepper (previous / next, "2 of 9").
 * The moving copy is aria-live off and its duplicate is aria-hidden.
 */
export default function Chyron({ entries, cleared, reduce, onPick }: Props) {
  const [paused, setPaused] = useState(false);
  const [step, setStep] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);
  const viewRef = useRef<HTMLDivElement>(null);
  const [manual, setManual] = useState<number | null>(null);
  const [dur, setDur] = useState(60);

  useLayoutEffect(() => {
    if (reduce || !listRef.current) return;
    const w = listRef.current.offsetWidth || 1;
    setDur(Math.max(30, w / 58));
  }, [entries, reduce]);

  useEffect(() => {
    if (step >= entries.length) setStep(0);
  }, [entries.length, step]);

  if (entries.length === 0) return null;
  const n = entries.length;
  const cur = ((step % n) + n) % n;

  const item = (e: ChyronEntry, i: number, dup: boolean) => (
    <li key={`${dup ? 'd' : 'a'}-${e.item_key}`} className={reduce && !dup && i === cur ? 'cur' : undefined}>
      <button
        type="button"
        className={`tk-item${cleared.has(e.item_key) ? ' is-cleared' : ''}`}
        tabIndex={dup ? -1 : undefined}
        onClick={() => onPick(e.item_key)}
      >
        <span className="tk-cat" style={{ ['--c' as string]: `var(--cat-${e.category})` }}>
          {CHYRON_LABEL[e.category]}
        </span>
        <span className="tk-text">{e.text}</span>
        <span className="tk-ref">{e.ref}</span>
      </button>
    </li>
  );

  const onFocusIn = (ev: React.FocusEvent) => {
    if (reduce || !listRef.current || !viewRef.current) return;
    const li = (ev.target as HTMLElement).closest('li');
    if (!li) return;
    const left = li.offsetLeft;
    const view = viewRef.current.offsetWidth;
    if (manual !== null && left >= manual && left + li.offsetWidth <= manual + view) return;
    setManual(Math.max(0, Math.min(left - 24, listRef.current.offsetWidth)));
  };
  const onFocusOut = (ev: React.FocusEvent) => {
    if (reduce) return;
    const next = ev.relatedTarget as Node | null;
    if (next && ev.currentTarget.contains(next)) return;
    setManual(null);
  };

  const trackStyle: React.CSSProperties = reduce
    ? {}
    : manual !== null
      ? { animation: 'none', transform: `translate3d(${-manual}px,0,0)` }
      : { ['--tk-dur' as string]: `${dur.toFixed(1)}s` };

  return (
    <section
      className={`ticker${paused ? ' paused' : ''}${reduce ? ' static' : ''}`}
      aria-label="Right now: the most important items on this page"
      onFocus={onFocusIn}
      onBlur={onFocusOut}
    >
      <span className="tk-label" aria-hidden="true"><i />Right now</span>
      <div className="tk-view" ref={viewRef} aria-live="off">
        <div className="tk-track" style={trackStyle}>
          <ul className="tk-list" ref={listRef}>{entries.map((e, i) => item(e, i, false))}</ul>
          {!reduce && <ul className="tk-list" aria-hidden="true">{entries.map((e, i) => item(e, i, true))}</ul>}
        </div>
      </div>
      <div className="tk-ctrl">
        {reduce && <span className="tk-count mono">{cur + 1} of {n}</span>}
        {reduce && (
          <button className="tk-btn tk-step" type="button" aria-label="Previous item" onClick={() => setStep(cur - 1)}>
            <Icon name="prev" />
          </button>
        )}
        {reduce && (
          <button className="tk-btn tk-step" type="button" aria-label="Next item" onClick={() => setStep(cur + 1)}>
            <Icon name="next" />
          </button>
        )}
        {!reduce && (
          <button
            className="tk-btn tk-pause"
            type="button"
            aria-label={paused ? 'Play the ticker' : 'Pause the ticker'}
            onClick={() => setPaused((p) => !p)}
          >
            <Icon name={paused ? 'play' : 'pause'} />
          </button>
        )}
      </div>
    </section>
  );
}
