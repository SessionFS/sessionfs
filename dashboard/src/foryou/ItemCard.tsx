import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { AttentionItem, MeetingPromotePreview } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { useForYouContext } from '../hooks/useForYou';
import { useLoops } from '../hooks/useReviewLoop';
import { createReviewLoopApi, type LoopBudgetPatch } from '../api/reviewLoop';
import { errorMessage } from '../utils/errorMessage';
import { ago, kindMeta, whenLabel } from './model';
import { Avatar, CountUp, Icon, Stamp } from './parts';

export interface StampState {
  label: string;
  tone: 'ok' | 'snz' | 'no';
}

/** What a card asks of the page. */
export interface CardApi {
  /** Done / hide / answered / read / not-an-action: leaves now, commits after the undo window. */
  act: (item: AttentionItem, action: 'done' | 'hide' | 'answered' | 'read' | 'dismiss') => void;
  /** Snooze menu (or "Remind me later" for meeting suggestions). */
  openSnooze: (item: AttentionItem, anchor: HTMLElement) => void;
  open: (item: AttentionItem) => void;
  /** A confirmed write (promote, reply, approve, raise, claim): runs now; the
   *  card leaves on success. ``run`` may return the toast text, or
   *  ``{ stamp, msg }`` to set the stamp from the server's answer. */
  commit: (item: AttentionItem, done: StampState & { msg: string }, run: () => Promise<unknown>) => Promise<boolean>;
  /** A short status toast (errors for actions without a panel). */
  notify?: (msg: string) => void;
  goto: (itemKey: string) => void;
  askHelper?: (item: AttentionItem) => void;
  meUserId?: string;
}

export interface CardHandle {
  toggleMore: () => void;
  toggleContext: () => boolean;
  main: () => void;
  focus: () => void;
}

const HOT = /^(overdue|due today|not answered|blocked|harmful|not claimed|not a ticket yet|you were named|no activity)/i;

function Reason({ why }: { why: string }) {
  const parts = why.split(' · ').filter(Boolean);
  return (
    <>
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && <span className="dot" aria-hidden="true">·</span>}
          <span className={HOT.test(p) || /\d+ harmful/.test(p) ? 'hot' : undefined}>{p}</span>
        </span>
      ))}
    </>
  );
}

type PanelKind = 'promote' | 'reply' | 'approve' | 'reject' | 'raise';
const PANEL_ACTIONS: PanelKind[] = ['approve', 'raise', 'promote', 'reply'];

function mainActionOf(item: AttentionItem): string | null {
  for (const a of [...PANEL_ACTIONS, 'claim']) if (item.actions.includes(a)) return a;
  return null;
}

export default function ItemCard({
  item,
  index,
  top,
  stamp,
  leaving,
  flash,
  reduce,
  api,
  register,
}: {
  item: AttentionItem;
  index: number;
  top: boolean;
  stamp: StampState | null;
  leaving: '' | 'right' | 'left' | 'down';
  flash: boolean;
  reduce: boolean;
  api: CardApi;
  register: (key: string, handle: CardHandle | null) => void;
}) {
  const { auth } = useAuth();
  const meta = kindMeta(item);
  const ctx = item.context ?? {};
  const uid = useId().replace(/:/g, '');
  const [moreOpen, setMoreOpen] = useState(false);
  const [ctxOpen, setCtxOpen] = useState(false);
  const [panel, setPanel] = useState<PanelKind | null>(null);
  // Phones show one primary button; the rest sit behind "More actions".
  const [mobOpen, setMobOpen] = useState(false);
  const cardRef = useRef<HTMLElement>(null);
  const mainRef = useRef<HTMLButtonElement>(null);
  const hasContext = item.actions.includes('context');
  const main = mainActionOf(item);

  useEffect(() => {
    register(item.item_key, {
      toggleMore: () => setMoreOpen((o) => !o),
      toggleContext: () => {
        if (!hasContext) return false;
        setCtxOpen((o) => !o);
        return true;
      },
      main: () => {
        if (main === 'claim') void claimHandoff(item, api, auth);
        else if (main) setPanel((p) => (p === main ? null : (main as PanelKind)));
        else api.open(item);
      },
      focus: () => cardRef.current?.focus(),
    });
    return () => register(item.item_key, null);
  }, [item, register, hasContext, main, api, auth]);

  const closePanel = () => {
    setPanel(null);
    requestAnimationFrame(() => mainRef.current?.focus());
  };

  const people = ctx.people ?? [];
  const when = item.due_at && (item.kind === 'reminder' || /overdue/.test(item.why))
    ? `Due ${whenLabel(item.due_at)}`
    : whenLabel(item.occurred_at);
  const srcLabel = ctx.source_label || meta.source;
  const leavingClass = leaving ? ` leaving${leaving === 'left' ? ' left' : leaving === 'down' ? ' down' : ''}` : '';

  return (
    <div
      className={`slot${leavingClass}${flash ? ' flash' : ''}`}
      id={`card-${item.item_key}`}
      style={{ ['--i' as string]: String(index) }}
    >
      <div className="slot-in">
        <article
          ref={cardRef}
          className={`card${ctx.harm || item.kind === 'loop_harmful_findings' ? ' harm' : ''}${top ? ' top' : ''}`}
          tabIndex={0}
          data-key={item.item_key}
          aria-labelledby={`t-${uid}`}
          aria-describedby={`w-${uid}`}
          style={{ ['--acc' as string]: meta.acc }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && e.target === e.currentTarget) {
              e.preventDefault();
              api.open(item);
            }
          }}
        >
          <span className="sheen" aria-hidden="true" />
          <div className="meta">
            <span className="kind"><Icon name={meta.icon} />{meta.label}</span>
            {(ctx.ref || item.source.id) && (
              <span className="srcchip" title={`${srcLabel} ${ctx.ref || item.source.id}`}>
                {srcLabel === meta.label ? <span className="sr-only">{srcLabel} </span> : <span className="st">{srcLabel}</span>}
                <span className="sid mono">{ctx.ref || item.source.id}</span>
              </span>
            )}
            {ctx.project_name && <span className="proj">{ctx.project_name}</span>}
            {item.kind === 'loop_harmful_findings' && <span className="harmtag">harmful</span>}
            {ctx.quiet_days != null && <span className="quietpill">quiet {ctx.quiet_days} days</span>}
            {when && <span className="when">{when}</span>}
          </div>
          <h3 className="title">
            <Link
              to={item.link}
              id={`t-${uid}`}
              onClick={(e) => {
                e.preventDefault();
                api.open(item);
              }}
            >
              {item.title}
            </Link>
          </h3>
          <p className="reason" id={`w-${uid}`}>
            <span className="sr-only">Why it is here: </span>
            <span><Reason why={item.why} /></span>
          </p>

          {ctx.quote && ctx.quote.text && (
            <blockquote className="quote card-quote">
              <cite>{ctx.quote.by}</cite>
              <p>{ctx.quote.text}</p>
            </blockquote>
          )}
          {ctx.progress && ctx.progress.length > 0 && (
            <div className="progs">
              {ctx.progress.map((p, i) => (
                <div className="prog bar" key={i}>
                  <div className="pl"><span>{p.label}</span><b>{p.text}</b></div>
                  <div className="ptrack" role="img" aria-label={`${p.label}: ${p.text}`}>
                    <i className="fill" style={{ ['--pt' as string]: String(p.value != null && p.max ? p.value / p.max : 1) }} />
                  </div>
                </div>
              ))}
            </div>
          )}
          {ctx.entries && ctx.entries.length > 0 && (
            <ul className="klist">
              {ctx.entries.map((e, i) => (
                <li key={i}>
                  <span className="ty">{e.type}</span>
                  <span>{e.text}</span>
                  <span className="by">{[e.by, e.at ? ago(e.at) : ''].filter(Boolean).join(' · ')}</span>
                </li>
              ))}
            </ul>
          )}
          {ctx.proof && <Proof item={item} reduce={reduce} />}
          {ctx.suggest && (
            <div className="suggest">
              <span className={`conf c${ctx.suggest.level}`}><i /><i /><i />{ctx.suggest.wording}</span>
              <dl>
                <div><dt>Suggested owner</dt><dd>{ctx.suggest.owner}</dd></div>
                <div><dt>Due</dt><dd>{ctx.suggest.due ? whenLabel(`${ctx.suggest.due}T09:00:00`, new Date(), false) : 'Not said'}</dd></div>
                <div><dt>Goes to</dt><dd>{ctx.suggest.destination.name}{ctx.suggest.destination.kind === 'group' ? ' (project group)' : ''}</dd></div>
              </dl>
            </div>
          )}

          <div className="people">
            {people.length > 0 && (
              <span className="avs">{people.map((p, i) => <Avatar key={i} person={p} />)}</span>
            )}
            <span className="pnames">
              {people.length > 0 && <span className="sr-only">People: </span>}
              {people.map((p, i) => (
                <span key={i}>
                  {i > 0 && <span className="dot" aria-hidden="true">·</span>}
                  <b>{p.name}</b> {p.role}
                </span>
              ))}
            </span>
            <button
              className="morebtn"
              type="button"
              aria-expanded={moreOpen}
              aria-controls={`more-${uid}`}
              onClick={() => setMoreOpen((o) => !o)}
            >
              More context<Icon name="chev" />
            </button>
          </div>

          <MoreContext item={item} id={`more-${uid}`} open={moreOpen} api={api} />
          {hasContext && ctxOpen && <BeforeYouStart item={item} id={`ctx-${uid}`} api={api} />}
          {panel === 'promote' && <PromotePanel item={item} id={`panel-${uid}`} api={api} onClose={closePanel} />}
          {panel === 'reply' && <ReplyPanel item={item} id={`panel-${uid}`} api={api} onClose={closePanel} />}
          {(panel === 'approve' || panel === 'reject') && (
            <DecidePanel key={panel} item={item} decision={panel} id={`panel-${uid}`} api={api} onClose={closePanel} />
          )}
          {panel === 'raise' && <RaisePanel item={item} id={`panel-${uid}`} api={api} onClose={closePanel} />}

          <div className={`actions${mobOpen ? ' mob-open' : ''}`} id={`acts-${uid}`}>
            <Actions
              item={item}
              uid={uid}
              ctxOpen={ctxOpen}
              panel={panel}
              mainRef={mainRef}
              onContext={() => setCtxOpen((o) => !o)}
              onPanel={(p) => setPanel((cur) => (cur === p ? null : p))}
              api={api}
            />
            <button
              className="btn mob-more"
              type="button"
              aria-expanded={mobOpen}
              aria-controls={`acts-${uid}`}
              onClick={() => setMobOpen((o) => !o)}
            >
              {mobOpen ? 'Fewer actions' : 'More actions'}
            </button>
          </div>
          {stamp && <Stamp label={stamp.label} tone={stamp.tone} />}
        </article>
      </div>
    </div>
  );
}

function Actions({
  item, uid, ctxOpen, panel, mainRef, onContext, onPanel, api,
}: {
  item: AttentionItem;
  uid: string;
  ctxOpen: boolean;
  panel: string | null;
  mainRef: React.RefObject<HTMLButtonElement | null>;
  onContext: () => void;
  onPanel: (p: PanelKind) => void;
  api: CardApi;
}) {
  const { auth } = useAuth();
  const a = item.actions;
  const out: ReactNode[] = [];
  const openLabel: Record<string, string> = {
    ticket: 'Open ticket',
    loop_harmful_findings: 'Open finding',
    approval: 'Open request',
    loop_ready: 'Review and merge',
    loop_needs_human: 'Open loop',
    loop_checklist: 'Open checklist',
    handoff: 'Open handoff',
    knowledge_changes: 'Open knowledge',
    proof: 'Open review loops',
  };
  if (a.includes('context')) {
    out.push(
      <button key="ctx" className="btn primary wide" type="button" aria-expanded={ctxOpen} aria-controls={`ctx-${uid}`} onClick={onContext}>
        {ctxOpen ? 'Hide context' : 'Before you start'} <kbd>b</kbd>
      </button>,
    );
  }
  if (a.includes('approve')) {
    out.push(
      <button key="approve" ref={mainRef} className="btn primary" type="button" aria-expanded={panel === 'approve'} aria-controls={`panel-${uid}`} onClick={() => onPanel('approve')}>
        Approve <kbd>a</kbd>
      </button>,
    );
  }
  if (a.includes('reject')) {
    out.push(
      <button key="reject" className="btn" type="button" aria-expanded={panel === 'reject'} aria-controls={`panel-${uid}`} onClick={() => onPanel('reject')}>
        Reject
      </button>,
    );
  }
  if (a.includes('raise')) {
    out.push(
      <button key="raise" ref={mainRef} className="btn primary" type="button" aria-expanded={panel === 'raise'} aria-controls={`panel-${uid}`} onClick={() => onPanel('raise')}>
        Raise limit <kbd>a</kbd>
      </button>,
    );
  }
  if (a.includes('claim')) {
    out.push(
      <button key="claim" ref={mainRef} className="btn primary" type="button" onClick={() => void claimHandoff(item, api, auth)}>
        Claim handoff <kbd>a</kbd>
      </button>,
    );
  }
  if (a.includes('promote')) {
    out.push(
      <button key="ack" ref={mainRef} className="btn primary" type="button" aria-expanded={panel === 'promote'} aria-controls={`panel-${uid}`} onClick={() => onPanel('promote')}>
        Acknowledge <kbd>a</kbd>
      </button>,
    );
  }
  if (a.includes('reply')) {
    out.push(
      <button key="reply" ref={mainRef} className="btn primary" type="button" aria-expanded={panel === 'reply'} aria-controls={`panel-${uid}`} onClick={() => onPanel('reply')}>
        Reply <kbd>a</kbd>
      </button>,
    );
  }
  if (a.includes('done')) {
    out.push(<button key="done" className="btn primary" type="button" onClick={() => api.act(item, 'done')}>Done <kbd>e</kbd></button>);
  }
  if (a.includes('read')) {
    out.push(<button key="read" className="btn primary" type="button" onClick={() => api.act(item, 'read')}>Mark as read <kbd>e</kbd></button>);
  }
  if (a.includes('answered')) {
    out.push(<button key="ans" className="btn" type="button" onClick={() => api.act(item, 'answered')}>Mark as answered <kbd>e</kbd></button>);
  }
  if (a.includes('open') || a.includes('context')) {
    const primary = !['context', 'done', 'read', 'promote', 'reply', 'approve', 'raise', 'claim'].some((x) => a.includes(x));
    out.push(
      <button key="open" className={`btn${primary ? ' primary' : item.kind === 'knowledge_changes' ? ' ghost' : ''}`} type="button" onClick={() => api.open(item)}>
        {openLabel[item.kind] ?? 'Open'}
      </button>,
    );
  }
  if (a.includes('remind')) {
    out.push(
      <button key="later" className="btn" type="button" aria-haspopup="menu" onClick={(e) => api.openSnooze(item, e.currentTarget)}>
        Remind me later <kbd>s</kbd>
      </button>,
    );
  }
  if (a.includes('dismiss')) {
    out.push(<button key="na" className="btn ghost" type="button" onClick={() => api.act(item, 'dismiss')}>Not an action <kbd>h</kbd></button>);
  }
  if (a.includes('snooze')) {
    out.push(
      <button key="snz" className="btn" type="button" aria-haspopup="menu" onClick={(e) => api.openSnooze(item, e.currentTarget)}>
        Snooze <kbd>s</kbd>
      </button>,
    );
  }
  if (a.includes('hide')) {
    out.push(
      <button key="hide" className="btn ghost" type="button" title="It comes back when something about it changes" onClick={() => api.act(item, 'hide')}>
        Hide until it changes <kbd>h</kbd>
      </button>,
    );
  }
  return <>{out}</>;
}

const FACTOR_LABEL = { band: 'Band', involvement: 'Involvement', urgency: 'Urgency', recency: 'Recency' } as const;

function MoreContext({ item, id, open, api }: { item: AttentionItem; id: string; open: boolean; api: CardApi }) {
  const ctx = item.context ?? {};
  const ref = ctx.ref || item.source.id;
  const facts: [string, string][] = [['Reference', ref], ...(ctx.facts ?? [])];
  const related = ctx.related ?? [];
  return (
    <div className={`more${open ? ' open' : ''}`} id={id} role="region" aria-label={`More context: ${item.title}`}>
      <div className="more-in">
        <div className="moregrid">
          {ctx.quote && ctx.quote.text && (
            <blockquote className="quote more-quote">
              <cite>{ctx.quote.by}</cite>
              <p>{ctx.quote.text}</p>
            </blockquote>
          )}
          <div>
            <div className="h6">Details</div>
            <dl className="facts">
              {facts.map(([k, v], i) => (
                <FactRow key={i} label={k} value={v} />
              ))}

            </dl>
          </div>
          <div>
            <div className="h6">Related <span>{related.length}</span></div>
            {related.length === 0 ? (
              <p className="none">Nothing else on this page is about the same project.</p>
            ) : (
              <ul className="related">
                {related.map((r) => (
                  <li key={r.item_key}>
                    <button type="button" tabIndex={open ? 0 : -1} onClick={() => api.goto(r.item_key)}>
                      <Icon name="link" /><span>{r.title}</span><em>{r.ref}</em>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="rankbox">
            <div className="h6">Why it ranks here</div>
            <ol className="rank">
              {item.rank.map((r) => (
                <li key={r.factor} className={r.decides ? 'decides' : undefined}>
                  <b>{FACTOR_LABEL[r.factor]}</b>
                  <span>
                    {r.factor === 'recency' && r.at ? `Latest activity ${whenLabel(r.at)}` : r.text}
                    {r.factor === 'urgency' && r.due_at && !/Overdue/.test(r.text) ? `, ${whenLabel(r.due_at, new Date(), false)}` : ''}
                    {r.decides && <em className="sr-only"> (decides the order)</em>}
                  </span>
                </li>
              ))}
            </ol>
            <p className="foot">Fixed rules, in this order. No learned weighting.</p>
          </div>
        </div>
      </div>
    </div>
  );
}

function FactRow({ label, value }: { label: string; value: string }) {
  const isDate = /^\d{4}-\d{2}-\d{2}T/.test(value);
  return (
    <>
      <dt>{label}</dt>
      <dd>{isDate ? whenLabel(value) : value}</dd>
    </>
  );
}

function BeforeYouStart({ item, id, api }: { item: AttentionItem; id: string; api: CardApi }) {
  const q = useForYouContext(item.item_key, true);
  if (q.isLoading) {
    return (
      <div className="ctx" id={id} aria-busy="true">
        <div><div className="h6">Related knowledge</div><span className="sk" style={{ width: '92%' }} /><span className="sk" style={{ width: '78%' }} /><span className="sk" style={{ width: '85%' }} /></div>
        <div><div className="h6">Past sessions</div><span className="sk" style={{ width: '100%', height: 44 }} /><span className="sk" style={{ width: '100%', height: 44 }} /></div>
        <span className="sr-only" role="status">Loading related knowledge and sessions</span>
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <div className="ctx" id={id}>
        <p className="emptyline" role="status">Couldn't load the context for this ticket.</p>
      </div>
    );
  }
  const { knowledge, sessions } = q.data;
  return (
    <div className="ctx" id={id}>
      <div>
        <div className="h6">Related knowledge <span className="mono">{knowledge.length}</span></div>
        {knowledge.length === 0 ? <p className="emptyline">No related knowledge yet.</p> : (
          <ul>
            {knowledge.map((k) => (
              <li className="claim" key={k.id}>
                <span className="ty">{k.entry_type}</span>
                <span>{k.content}</span>
                <span className="cf">{k.created_at ? whenLabel(k.created_at) : ''}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <div className="h6">Past sessions <span className="mono">{sessions.length}</span></div>
        {sessions.length === 0 ? <p className="emptyline">None of your sessions match yet.</p> : (
          <ul>
            {sessions.map((s) => (
              <li key={s.id}>
                <Link className="sess" to={s.link}>
                  <span className="tool">{s.source_tool}</span>
                  <span>{s.title || 'Untitled session'}</span>
                  <span className="m">{s.updated_at ? whenLabel(s.updated_at) : ''}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="askrow">
        {api.askHelper && (
          <button className="btn sm" type="button" onClick={() => api.askHelper?.(item)}>Ask the helper about this area</button>
        )}
        <span>Fetched when you opened this card. Access re-checked.</span>
      </div>
    </div>
  );
}

function PromotePanel({ item, id, api, onClose }: { item: AttentionItem; id: string; api: CardApi; onClose: () => void }) {
  const { auth } = useAuth();
  const m = item.context.meeting!;
  const sg = item.context.suggest!;
  const [preview, setPreview] = useState<MeetingPromotePreview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [title, setTitle] = useState(sg.title);
  const [target, setTarget] = useState<string>('');
  const [assignee, setAssignee] = useState<'suggested' | 'none'>('suggested');
  const [due, setDue] = useState(sg.due ?? '');
  const [priority, setPriority] = useState<'medium' | 'high' | 'low' | 'critical'>('medium');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const titleRef = useRef<HTMLInputElement>(null);
  const uid = useId().replace(/:/g, '');
  const cited = m.range ?? `L${m.lines}`;

  useEffect(() => {
    let live = true;
    auth!.client
      .previewMeetingPromote(m.id, m.item_id)
      .then((p) => {
        if (!live) return;
        setPreview(p);
        setTarget(p.target_project_id ?? p.target_project_options?.[0]?.id ?? '');
        if (p.due_date) setDue(p.due_date);
      })
      .catch((e) => live && setLoadError(errorMessage(e)));
    titleRef.current?.focus();
    return () => {
      live = false;
    };
  }, [auth, m.id, m.item_id]);

  const suggestedLabel = preview
    ? preview.assigned_user_id
      ? preview.assigned_user_id === api.meUserId ? 'You' : 'The suggested owner'
      : preview.assigned_to ? `${preview.assigned_to} (persona)` : null
    : null;
  const options = preview?.target_project_options ?? null;
  const why = sg.destination.kind === 'group'
    ? `${sg.destination.name} is pre-selected because ${m.title} belongs to that project group.`
    : `${sg.destination.name} is pre-selected because ${m.title} is attached to that project.`;

  const submit = async () => {
    if (!title.trim()) {
      setError('Add a title for the ticket.');
      titleRef.current?.focus();
      return;
    }
    if (!preview) return;
    if (options && !target) {
      setError('Pick the project the ticket goes to.');
      return;
    }
    setBusy(true);
    setError(null);
    const ok = await api.commit(
      item,
      { label: 'Ticket created', tone: 'ok', msg: '' },
      async () => {
        const res = await auth!.client.promoteMeetingItem(m.id, m.item_id, {
          title: title.trim(),
          description: preview.description,
          priority,
          kind: preview.kind,
          assigned_to: assignee === 'suggested' ? preview.assigned_to : null,
          assigned_user_id: assignee === 'suggested' ? preview.assigned_user_id : null,
          due_date: due || null,
          target_project_id: options ? target : preview.target_project_id,
        });
        return `Created ${res.ticket_id}. It links back to ${m.title} ${cited}.`;
      },
    ).catch((e) => {
      setError(errorMessage(e));
      return false;
    });
    setBusy(false);
    if (!ok) titleRef.current?.focus();
  };

  return (
    <div className="panel" id={id}>
      <h4 className="ph">Turn this into a ticket</h4>
      <p>The ticket links back to {m.title} {cited}, so anyone with access can read the cited {m.kind === 'chat' ? 'messages' : 'lines'}.</p>
      {loadError && <span className="err" role="alert">{loadError}</span>}
      <div className="form">
        <label className="fld wide">
          <span>Title</span>
          <input ref={titleRef} type="text" value={title} maxLength={200} aria-invalid={!!error && !title.trim()} onChange={(e) => { setTitle(e.target.value); setError(null); }} />
        </label>
        <label className="fld">
          <span>{options ? `Project in ${sg.destination.name}` : 'Project'}</span>
          {options ? (
            <select value={target} onChange={(e) => setTarget(e.target.value)} disabled={!preview}>
              {options.length === 0 && <option value="">No project you can edit</option>}
              {options.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          ) : (
            <select value="fixed" disabled>
              <option value="fixed">{sg.destination.name}</option>
            </select>
          )}
        </label>
        <label className="fld">
          <span>Assignee</span>
          <select value={assignee} onChange={(e) => setAssignee(e.target.value as 'suggested' | 'none')} disabled={!preview}>
            {suggestedLabel && <option value="suggested">{suggestedLabel}</option>}
            <option value="none">Unassigned</option>
          </select>
        </label>
        <label className="fld">
          <span>Due date</span>
          <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
        </label>
        <label className="fld">
          <span>Priority</span>
          <select value={priority} onChange={(e) => setPriority(e.target.value as typeof priority)}>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="low">Low</option>
            <option value="critical">Critical</option>
          </select>
        </label>
      </div>
      <p className="hint"><Icon name="suspect" />{why}</p>
      {error && <span className="err" role="alert" id={`err-${uid}`}>{error}</span>}
      <div className="row">
        <button className="btn primary" type="button" disabled={busy || !preview} onClick={submit}>{busy ? 'Creating…' : 'Create ticket'}</button>
        <button className="btn ghost" type="button" onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}

function ReplyPanel({ item, id, api, onClose }: { item: AttentionItem; id: string; api: CardApi; onClose: () => void }) {
  const { auth } = useAuth();
  const reply = item.context.reply!;
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const uid = useId().replace(/:/g, '');
  useEffect(() => ref.current?.focus(), []);
  const first = reply.to.split(' ')[0];
  const send = async () => {
    if (!text.trim()) {
      setError('Write a reply first.');
      ref.current?.focus();
      return;
    }
    setBusy(true);
    const ok = await api
      .commit(item, { label: 'Replied', tone: 'ok', msg: '' }, async () => {
        await auth!.client.addTicketComment(reply.project_id, reply.ticket_id, text.trim());
        return `Reply sent. ${first} is notified.`;
      })
      .catch((e) => {
        setError(errorMessage(e));
        return false;
      });
    setBusy(false);
    if (!ok) ref.current?.focus();
  };
  return (
    <div className="panel" id={id}>
      <h4 className="ph">Reply to {first} on the ticket</h4>
      <p>Posted as you. The card leaves once the reply is sent.</p>
      <label className="sr-only" htmlFor={`rpl-${uid}`}>Reply</label>
      <textarea
        id={`rpl-${uid}`}
        ref={ref}
        placeholder="Write a reply"
        value={text}
        aria-invalid={!!error}
        onChange={(e) => { setText(e.target.value); setError(null); }}
        onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); onClose(); } }}
      />
      {error && <span className="err" role="alert">{error}</span>}
      <div className="row">
        <button className="btn primary" type="button" disabled={busy} onClick={send}>{busy ? 'Sending…' : 'Send reply'}</button>
        <button className="btn ghost" type="button" onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}

type Auth = ReturnType<typeof useAuth>['auth'];

/** Claim a handoff straight from its card (POST /handoffs/{id}/claim). */
async function claimHandoff(item: AttentionItem, api: CardApi, auth: Auth): Promise<void> {
  await api
    .commit(item, { label: 'Claimed', tone: 'ok', msg: '' }, async () => {
      await auth!.client.claimHandoff(item.source.id);
      return 'Handoff claimed. The session is in your sessions now.';
    })
    .catch((e) => {
      api.notify?.(`Couldn’t claim it: ${errorMessage(e)}`);
      return false;
    });
}

function DecidePanel({
  item, decision, id, api, onClose,
}: { item: AttentionItem; decision: 'approve' | 'reject'; id: string; api: CardApi; onClose: () => void }) {
  const { auth } = useAuth();
  const ap = item.context.approval;
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const uid = useId().replace(/:/g, '');
  useEffect(() => ref.current?.focus(), []);
  const isA = decision === 'approve';
  const got = ap?.approved_count ?? 0;
  const need = ap?.required_count ?? 1;
  const target = ap?.target_label ?? 'this change';
  const policy = ap?.policy_name ?? 'this policy';
  const lead = isA
    ? got + 1 >= need
      ? `Yours is the ${need === 1 ? 'only approval needed' : `last of ${need}`}, so the request is approved once it is recorded.`
      : `${got} of ${need} so far. Yours makes ${got + 1} of ${need}.`
    : 'One reject rejects the whole request. The author sees your reason; a new commit or edit starts a new request.';
  const submit = async () => {
    if (busy) return;
    if (!reason.trim()) {
      setError(isA ? 'Add a reason. It is kept with your approval in the audit trail.' : 'Add a reason so the author knows what to change.');
      ref.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    const ok = await api
      .commit(
        item,
        isA ? { label: 'Approved', tone: 'ok', msg: '' } : { label: 'Rejected', tone: 'no', msg: '' },
        async () => {
          const res = await auth!.client.decideApprovalRequest(item.source.id, { decision, reason: reason.trim() });
          if (!isA) return `Rejected. ${policy} on ${target} is closed.`;
          const k = res.approved_count ?? got + 1;
          const n = res.required_count ?? need;
          return {
            stamp: `Approved · ${k} of ${n}`,
            msg: res.state === 'approved' ? `Approved. ${policy} has every approval it needs.` : `Approved: ${k} of ${n}.`,
          };
        },
      )
      .catch((e) => {
        setError(errorMessage(e));
        return false;
      });
    setBusy(false);
    if (!ok) ref.current?.focus();
  };
  return (
    <div className="panel" id={id} aria-busy={busy || undefined}>
      <h4 className="ph">{isA ? 'Approve' : 'Reject'} {target} for {policy}</h4>
      <p>{lead}</p>
      {ap?.target_title && <p className="hint">{ap.target_title}</p>}
      <label className="sr-only" htmlFor={`rsn-${uid}`}>Reason</label>
      <textarea
        id={`rsn-${uid}`}
        ref={ref}
        placeholder={isA ? 'Reason, for example: secret read path reviewed, the value is never logged' : 'Reason: what needs to change'}
        value={reason}
        maxLength={2000}
        disabled={busy}
        aria-invalid={!!error}
        aria-describedby={error ? `rsnerr-${uid}` : undefined}
        onChange={(e) => { setReason(e.target.value); setError(null); }}
        onKeyDown={(e) => { if (e.key === 'Escape' && !busy) { e.stopPropagation(); onClose(); } }}
      />
      {error && <span className="err" role="alert" id={`rsnerr-${uid}`}>{error}</span>}
      <div className="row">
        <button className={`btn ${isA ? 'primary' : 'danger'}`} type="button" disabled={busy} onClick={submit}>
          {busy ? (isA ? 'Approving…' : 'Rejecting…') : isA ? 'Approve' : 'Reject with this reason'}
        </button>
        <button className="btn ghost" type="button" disabled={busy} onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}

function fmtTokens(n: number): string {
  return n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(2)}M` : n >= 1_000 ? `${Math.round(n / 1_000)}k` : String(n);
}

function RaisePanel({ item, id, api, onClose }: { item: AttentionItem; id: string; api: CardApi; onClose: () => void }) {
  const { auth } = useAuth();
  const lp = item.context.loop!;
  const loops = useLoops(lp.project_id);
  const loopApi = useMemo(() => (auth ? createReviewLoopApi(auth.baseUrl, auth.apiKey) : null), [auth]);
  const loop = loops.data?.find((l) => l.id === lp.id);
  const child = loop?.children.find((c) => c.ticket_id === lp.ticket_id) ?? loop?.children[0];
  const [pick, setPick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const firstRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (child) firstRef.current?.focus();
  }, [child]);
  const choices: { label: string; body: LoopBudgetPatch }[] = [];
  if (child) {
    if (lp.limit === 'rounds') {
      const cur = child.budget.max_rounds ?? child.round;
      for (const d of [2, 4]) choices.push({ label: `+${d} rounds (cap ${cur + d})`, body: { max_review_rounds: cur + d } });
    } else {
      const cur = child.budget.tokens ?? child.tokens;
      for (const d of [500_000, 1_000_000]) choices.push({ label: `+${fmtTokens(d)} tokens (cap ${fmtTokens(cur + d)})`, body: { token_budget: cur + d } });
    }
  }
  const used = child
    ? lp.limit === 'rounds'
      ? `${child.round} of ${child.budget.max_rounds ?? child.round} review rounds are used.`
      : `${fmtTokens(child.tokens)} of ${fmtTokens(child.budget.tokens ?? child.tokens)} tokens are used.`
    : '';
  const submit = async () => {
    if (busy || !choices[pick] || !loopApi) return;
    setBusy(true);
    setError(null);
    await api
      .commit(item, { label: 'Loop resumed', tone: 'ok', msg: '' }, async () => {
        await loopApi.updateLoop(lp.project_id, lp.id, choices[pick].body);
        return `Limit raised. ${loop?.title ?? 'The loop'} runs its next round.`;
      })
      .catch((e) => {
        setError(errorMessage(e));
        return false;
      });
    setBusy(false);
  };
  return (
    <div className="panel" id={id} aria-busy={busy || loops.isLoading || undefined}>
      <h4 className="ph">Raise the limit and resume the loop</h4>
      {loops.isLoading && <p role="status">Loading the loop’s limits…</p>}
      {loops.isError && <span className="err" role="alert">{errorMessage(loops.error)}</span>}
      {!loops.isLoading && !loops.isError && !child && <span className="err" role="alert">This loop is no longer open to you.</span>}
      {child && (
        <>
          <p>The limit belongs to the whole loop, so it applies to every item in {loop!.title}. {used}</p>
          <div className="chips" role="radiogroup" aria-label="New limit">
            {choices.map((c, i) => (
              <button
                key={c.label}
                ref={i === 0 ? firstRef : undefined}
                className="chip"
                type="button"
                role="radio"
                aria-checked={pick === i}
                tabIndex={pick === i ? 0 : -1}
                disabled={busy}
                onClick={() => setPick(i)}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); setPick((i + 1) % choices.length); }
                  if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); setPick((i - 1 + choices.length) % choices.length); }
                }}
              >
                {c.label}
              </button>
            ))}
          </div>
        </>
      )}
      {error && <span className="err" role="alert">{error}</span>}
      <div className="row">
        <button className="btn primary" type="button" disabled={busy || !child} onClick={submit}>{busy ? 'Raising…' : 'Raise limit and resume'}</button>
        <button className="btn ghost" type="button" disabled={busy} onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}

function Proof({ item, reduce }: { item: AttentionItem; reduce: boolean }) {
  const p = item.context.proof!;
  const perTokens = p.tokens_per_accepted_change ?? null;
  const dollars = item.context.dollars_visible ? p.cost_usd_per_accepted_change ?? null : null;
  const days = p.days ?? [];
  const label = days.map((d) => `${new Date(`${d.date}T12:00:00`).toLocaleDateString('en-GB', { weekday: 'long' })} ${d.rounds}`).join(', ');
  let j = 0;
  return (
    <>
      <div className="proof">
        <div>
          <span className="big"><CountUp value={p.caught} reduce={reduce} /></span>
          <span className="cap">harmful bugs stopped before merge</span>
        </div>
        <div>
          <span className="big">
            {perTokens == null ? '—' : perTokens >= 100_000
              ? <><CountUp value={perTokens / 1_000_000} decimals={1} reduce={reduce} /><small>M</small></>
              : <><CountUp value={perTokens / 1_000} decimals={1} reduce={reduce} /><small>k</small></>}
          </span>
          <span className="cap">tokens per accepted change</span>
        </div>
        <div>
          <span className="big">{dollars == null ? <CountUp value={p.accepted_changes} reduce={reduce} /> : <CountUp value={dollars} decimals={2} prefix="$" reduce={reduce} />}</span>
          <span className="cap">{dollars == null ? 'accepted changes' : 'per accepted change'}</span>
        </div>
      </div>
      {days.length > 0 && (
        <>
          <div className="week" role="img" aria-label={`Review rounds per day: ${label}`}>
            {days.map((d) => {
              const bars = [];
              for (let i = 0; i < Math.min(d.rounds, 8); i++) {
                bars.push(<i key={i} className={i < d.caught ? 'hb' : ''} style={{ ['--j' as string]: String(j++) }} />);
              }
              return (
                <div className="d" key={d.date}>
                  <div className="stack">{bars}</div>
                  <span className="lab">{new Date(`${d.date}T12:00:00`).toLocaleDateString('en-GB', { weekday: 'short' })}</span>
                </div>
              );
            })}
          </div>
          <div className="weekkey">
            <span><i style={{ background: 'var(--text-primary)' }} />Caught a harmful bug</span>
            <span><i style={{ background: 'var(--border-strong)' }} />Other rounds</span>
          </div>
        </>
      )}
      <p className="adminnote">
        {p.rounds} round{p.rounds === 1 ? '' : 's'}, {p.accepted_changes} accepted change{p.accepted_changes === 1 ? '' : 's'}. Dollar amounts show only to org owners and admins.
      </p>
    </>
  );
}
