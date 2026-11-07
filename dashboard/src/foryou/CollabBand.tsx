import { useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import type { CollabFlow, CollabNode, CollabProject } from '../api/client';
import { useRemindCollaborator } from '../hooks/useForYou';
import { errorMessage } from '../utils/errorMessage';
import { ago, hhmm, localZone } from './model';
import { Avatar, CountUp, Icon, Stamp } from './parts';

/** "Working with others": per project, who you work with and what your work is waiting for. */
export default function CollabBand({
  projects,
  waiting,
  error,
  reduce,
  toast,
  onRetry,
}: {
  onRetry?: () => void;
  projects: CollabProject[];
  waiting: number;
  error: string | null;
  reduce: boolean;
  toast: (msg: string) => void;
}) {
  const flows = projects.flatMap((p) => p.flows);
  const blocked = flows.filter((f) => f.state === 'dependency').length;
  return (
    <section className="band" data-band="together" id="band-together" aria-labelledby="h-together" style={{ ['--acc' as string]: 'var(--brand)' }}>
      <div className="bandhead">
        <h2 id="h-together" tabIndex={-1}>Working with others</h2>
        <span className="cnt"><CountUp value={waiting} reduce={reduce} /> waiting on others</span>
        <p className="sub">
          Projects where you work with people and agents, and what your work is waiting for: {blocked} on another
          ticket, {flows.length - blocked} on a reviewer.
        </p>
      </div>
      {error && (
        <div className="banderr" role="status">
          <span>Working with others couldn’t load.</span>
          {onRetry && <button className="btn sm" type="button" onClick={onRetry}>Retry</button>}
        </div>
      )}
      {!error && projects.length === 0 && (
        <p className="bandempty">Nothing of yours is waiting on anyone else.</p>
      )}
      <div className="collabs">
        {projects.map((c, ci) => {
          const people = c.people.filter((p) => !p.agent).length;
          const agents = c.people.length - people;
          return (
            <article className="collab" key={c.project_id} style={{ ['--i' as string]: String(ci) }} aria-labelledby={`cp-${c.project_id}`}>
              <div className="chead">
                <h3 id={`cp-${c.project_id}`}>{c.project_name}</h3>
                {c.group_name && <span className="gchip">group: {c.group_name}</span>}
                <span className="cmeta">
                  {people} {people === 1 ? 'person' : 'people'} · {agents} {agents === 1 ? 'agent' : 'agents'} · {c.flows.length} of yours in flight
                </span>
              </div>
              {c.people.length > 0 && (
                <ul className="who" aria-label={`Working with you on ${c.project_name}`}>
                  {c.people.map((p, i) => (
                    <li key={i} className={p.agent ? 'ag' : undefined}>
                      <Avatar person={p} />
                      <span><b>{p.name}</b> {p.role}</span>
                    </li>
                  ))}
                </ul>
              )}
              {c.flows.length > 0 && (
                <>
                  <h4>Your work in flight</h4>
                  <ol className="flows">
                    {c.flows.map((f) => <FlowRow key={f.flow_key} flow={f} reduce={reduce} toast={toast} />)}
                  </ol>
                </>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function nodeWho(n: CollabNode): string {
  const parts = [n.who.name];
  if (n.kind === 'rev') parts.push(n.since ? `requested · ${ago(n.since)}` : 'requested');
  else parts.push(n.status.replace('_', ' ') + (n.since && n.kind !== 'mine' ? ` · ${ago(n.since)}` : ''));
  return parts.join(' · ');
}

function FlowRow({ flow, reduce, toast }: { flow: CollabFlow; reduce: boolean; toast: (msg: string) => void }) {
  const uid = useId().replace(/:/g, '');
  const [open, setOpen] = useState(false);
  const [msg, setMsg] = useState(flow.default_message);
  const [when, setWhen] = useState<'now' | 'tomorrow'>('now');
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const [stamp, setStamp] = useState<string | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const remind = useRemindCollaborator();
  const first = flow.to?.first ?? '';
  const isDep = flow.state === 'dependency';

  const detail = isDep && flow.blocking
    ? (() => {
        const owner = flow.chain[1]?.who.name ?? 'Nobody yet';
        const stale = flow.chain[1]?.stale;
        const age = flow.blocking.since ? ago(flow.blocking.since) : '';
        return (
          <>
            {flow.blocking.title} · {flow.blocking.owner_role} {owner} ·{' '}
            {stale ? <span className="stale">not started for {age}</span> : `${flow.blocking.status.replace('_', ' ')}${age ? ` for ${age}` : ''}`}
          </>
        );
      })()
    : (
      <>
        {flow.to?.name ?? flow.reviewer_persona ?? 'No reviewer named yet'} · round {flow.round ?? 1} requested
        {flow.since ? ` · waiting ${ago(flow.since)}` : ''}
        {flow.last_verdict ? ` · last verdict ${flow.last_verdict}` : ''}
      </>
    );

  const chainLabel = flow.chain.map((n) => `${n.ref} (${n.who.name}, ${n.status})`).join(', which waits on ');

  const close = (refocus: boolean) => {
    setOpen(false);
    setError(null);
    if (refocus) requestAnimationFrame(() => btnRef.current?.focus());
  };

  const send = () => {
    if (!msg.trim()) {
      setError('Write a message first.');
      return;
    }
    remind.mutate(
      { flow_key: flow.flow_key, message: msg.trim(), when, timezone: localZone() },
      {
        onSuccess: (res) => {
          const label = when === 'now' ? `Reminder sent to ${first}` : `Reminder to ${first} set for tomorrow 09:00`;
          close(false);
          setStamp(label);
          window.setTimeout(() => {
            setStamp(null);
            setSent(when === 'now' ? `You reminded ${first} at ${hhmm(new Date())}` : `Goes to ${first} tomorrow at 09:00`);
            btnRef.current?.focus();
          }, reduce ? 0 : 1300);
          toast(`${label}. ${when === 'now' ? `${res.to.split(' ')[0]} sees it in SessionFS and by email.` : 'You can cancel it from Reminders.'}`);
        },
        onError: (e) => setError(errorMessage(e)),
      },
    );
  };

  return (
    <li className="flow">
      <div className="fhead">
        <span className={`fstate ${isDep ? 'dep' : 'onrev'}`}><Icon name={isDep ? 'lock' : 'eye'} />{flow.label}</span>
        <span className="fref">{flow.ticket.id}</span>
      </div>
      <p className="ftitle"><Link to={flow.ticket.link}>{flow.ticket.title}</Link></p>
      <p className="fdet">{detail}</p>
      <ol className="chain" aria-label={`Chain: ${chainLabel}`}>
        {flow.chain.map((n, i) => (
          <ChainNode key={`${n.kind}-${n.ref}`} node={n} first={i === 0} />
        ))}
      </ol>
      <div className="factions">
        {flow.can_remind && flow.to ? (
          <button
            ref={btnRef}
            className="btn sm"
            type="button"
            aria-expanded={open}
            aria-controls={`cmp-${uid}`}
            onClick={() => (open ? close(true) : setOpen(true))}
          >
            <Icon name="bell" />
            <span>{sent ? `Remind ${first} again` : `Send a reminder to ${first}`}</span>
          </button>
        ) : (
          <span className="fnote">{isDep ? 'Nobody owns the blocking ticket yet.' : 'No reviewer is named yet.'}</span>
        )}
        {sent && <span className="fsent"><Icon name="check" />{sent}</span>}
      </div>
      {open && flow.to && (
        <div className="composer" id={`cmp-${uid}`}>
          <h5><Avatar person={{ name: flow.to.name, agent: false }} />Reminder to {flow.to.name}</h5>
          <label className="sr-only" htmlFor={`cmt-${uid}`}>Message</label>
          <textarea
            id={`cmt-${uid}`}
            value={msg}
            autoFocus
            maxLength={4000}
            aria-invalid={!!error}
            onChange={(e) => { setMsg(e.target.value); setError(null); }}
            onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); close(true); } }}
          />
          {error && <span className="err" role="alert">{error}</span>}
          <div className="row">
            <span className="lab" id={`when-${uid}`}>Send</span>
            <div className="chips" role="radiogroup" aria-labelledby={`when-${uid}`}>
              {(['now', 'tomorrow'] as const).map((w) => (
                <button
                  key={w}
                  className="chip"
                  type="button"
                  role="radio"
                  aria-checked={when === w}
                  tabIndex={when === w ? 0 : -1}
                  onClick={() => setWhen(w)}
                  onKeyDown={(e) => {
                    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                      e.preventDefault();
                      setWhen(w === 'now' ? 'tomorrow' : 'now');
                    }
                  }}
                >
                  {w === 'now' ? 'Now' : 'Tomorrow 09:00'}
                </button>
              ))}
            </div>
          </div>
          <div className="row">
            <button className="btn primary" type="button" disabled={remind.isPending} onClick={send}>
              {remind.isPending ? 'Sending…' : `Send reminder to ${first}`}
            </button>
            <button className="btn ghost" type="button" onClick={() => close(true)}>Cancel</button>
          </div>
        </div>
      )}
      {stamp && <Stamp label={stamp} tone={when === 'now' ? 'ok' : 'snz'} />}
    </li>
  );
}

function ChainNode({ node, first }: { node: CollabNode; first: boolean }) {
  return (
    <>
      {!first && (
        <li className={`link${node.kind === 'rev' ? ' rev' : ''}`} aria-hidden="true">
          <span>{node.kind === 'rev' ? 'review by' : 'waits on'}</span>
          <span className="ln" />
        </li>
      )}
      <li className={`node ${node.kind}`}>
        <span className="nid" title={node.ref}>{node.ref}</span>
        <span className="nt" title={node.title}>{node.title}</span>
        <span className="nw">
          <Avatar person={node.who} small />
          <span className={node.stale ? 'stale' : undefined}>{nodeWho(node)}</span>
        </span>
      </li>
    </>
  );
}
