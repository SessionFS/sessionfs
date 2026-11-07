import { forwardRef } from 'react';
import { Link } from 'react-router-dom';
import type { AttentionItem } from '../api/client';
import { hhmm, whenLabel, type ClearedEntry } from './model';

/** "You're all caught up": the seal, today's trail and what comes next. */
const CaughtUp = forwardRef<HTMLElement, {
  cleared: ClearedEntry[];
  next: AttentionItem[];
  play: boolean;
  onLately: () => void;
}>(function CaughtUp({ cleared, next, play, onLately }, ref) {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 8, 0, 0).getTime();
  const span = 10 * 3_600_000; // 08:00–18:00
  const pos = (t: number) => Math.max(0, Math.min(100, ((t - start) / span) * 100));
  const urgent = cleared.filter((c) => c.group === 'radar').length;
  const firstAt = cleared.length ? Math.min(...cleared.map((c) => c.at)) : now.getTime();
  const mins = Math.max(1, Math.round((now.getTime() - firstAt) / 60_000));
  const took = mins < 60 ? `${mins} min` : `${Math.floor(mins / 60)} h ${mins % 60} min`;
  const lead = cleared.length
    ? `You cleared ${cleared.length} item${cleared.length === 1 ? '' : 's'}${urgent ? `, including ${urgent} on your radar` : ''}, in ${took}. Nothing else is waiting on you.`
    : 'Everything that needed you is handled. Nothing is waiting on you.';
  const kindLabel = (it: AttentionItem) =>
    it.kind === 'reminder' ? 'Next reminder' : it.kind === 'ticket' ? 'Ticket due' : it.kind === 'handoff' ? 'Handoff' : 'Coming up';
  return (
    <section ref={ref} className={`caught${play ? ' play' : ''}`} aria-labelledby="caughtH" tabIndex={-1}>
      <svg className="seal" viewBox="0 0 132 132" aria-hidden="true">
        <circle className="ring" cx="66" cy="66" r="62" />
        <circle className="ring" cx="66" cy="66" r="48" style={{ strokeDasharray: '2 5' }} />
        <path className="arc" d="M66 4 A62 62 0 0 1 128 66" />
        <path className="arc arc2" d="M66 128 A62 62 0 0 1 4 66" />
        <circle className="ripple" cx="66" cy="66" r="30" />
        <g className="br-l"><path className="br" d="M52,40 Q38,40 38,54 L38,78 Q38,92 52,92" /></g>
        <g className="br-r"><path className="br" d="M80,40 Q94,40 94,54 L94,78 Q94,92 80,92" /></g>
        <circle className="core" cx="66" cy="66" r="8" />
      </svg>
      <h2 id="caughtH" className="ct">You&rsquo;re all caught up.</h2>
      <p className="lead ct2">{lead}</p>
      <div className="trail ct3" role="img" aria-label={`What you cleared today: ${cleared.length} item${cleared.length === 1 ? '' : 's'}`}>
        <div className="tl"><span>Cleared today: {cleared.length}</span><span className="mono">now {hhmm(now)}</span></div>
        <div className="line">
          {cleared.map((c, i) => (
            <span
              key={`${c.key}-${i}`}
              className={`tick ${c.group}-b`}
              style={{ left: `${pos(c.at).toFixed(2)}%`, animationDelay: `${(1.3 + i * 0.07).toFixed(2)}s` }}
              title={`${c.title} · ${hhmm(new Date(c.at))}`}
            />
          ))}
          <span className="now" style={{ left: `${pos(now.getTime()).toFixed(2)}%` }} />
        </div>
        <div className="hours"><span>08:00</span><span>10:00</span><span>12:00</span><span>14:00</span><span>16:00</span><span>18:00</span></div>
      </div>
      {next.length > 0 && (
        <div className="next ct3" role="list" aria-label="Coming up next">
          <div className="nh" aria-hidden="true">Coming up next</div>
          {next.map((it) => (
            <div className="ni" role="listitem" key={it.item_key}>
              <span className="k">{kindLabel(it)}</span>
              <span>{it.title}</span>
              <span className="w">{whenLabel(it.due_at || it.occurred_at)}</span>
            </div>
          ))}
        </div>
      )}
      <div className="row ct3">
        <button className="btn" type="button" onClick={onLately}>Read what changed this week</button>
        <Link className="btn ghost" to="/sessions">Open Sessions</Link>
      </div>
    </section>
  );
});

export default CaughtUp;
