/**
 * The session-helper drawer: the Enterprise "ask over your sessions" panel.
 *
 * Streaming is done with raw `fetch` + `ReadableStream` (EventSource can't set
 * the `Authorization` header), the chunks are framed by `SseParser`, and the
 * text deltas are rendered progressively through the shared `ui/Markdown`.
 * Tool calls/results and citations are folded into the assistant turn as they
 * arrive. Layout: pinned header (scope + history/new/close), a scrolling
 * transcript (or the history list), and a pinned composer.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { ApiError, type HelperCitation, type HelperConversationSummary } from '../api/client';
import { useMe } from '../hooks/useMe';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { Button, Dialog, DialogFooter, DialogHeader, Drawer } from '../components/ui';
import type { HelperScope } from './HelperProvider';
import { useDeleteHelperConversation, useHelperConversations, useHelperStatus } from './useHelper';
import { SseParser, type SseEvent } from './sse';
import {
  MAX_MESSAGE_CHARS,
  TITLE_ID,
  classifyError,
  streamError,
  type HelperError,
  type ToolStep,
  type Turn,
} from './format';
import { activeOption, hasScope, scopeKey, scopeOptions, type ScopeOption } from './scope';
import HelperHeader from './HelperHeader';
import HelperComposer from './HelperComposer';
import HelperEmptyState from './HelperEmptyState';
import HelperHistory from './HelperHistory';
import HelperNotice from './HelperNotice';
import { AssistantMessage, UserMessage } from './HelperMessage';
import { AlertIcon, ArrowDownIcon, KeyIcon, LockIcon } from './icons';
import { linkButtonPrimary, linkButtonSecondary } from './styles';

const STICK_THRESHOLD_PX = 72;

function keyLabel(source: string | null | undefined): string | null {
  if (source === 'user') return 'Your key';
  if (source === 'org') return 'Org key';
  return null;
}

/** The status endpoint's 4xx scope failures (a scope the user switched to or a
 *  restored thread's scope is gone / mixed-org), versus a transient failure. */
function classifyStatusError(err: unknown): { message: string } | null {
  if (err instanceof ApiError && err.status === 404) {
    return { message: "You no longer have access to this conversation's scope." };
  }
  if (err instanceof ApiError && err.status === 400) {
    return { message: errorMessage(err) };
  }
  return null;
}

export default function HelperPanel({
  open,
  onClose,
  scope,
}: {
  open: boolean;
  onClose: () => void;
  scope?: HelperScope;
}) {
  const { auth } = useAuth();
  const { addToast } = useToast();
  const me = useMe();

  // Declared before the status hook so availability follows the *active* scope
  // (the entry scope on open, then whatever changeScope / history restore sets)
  // rather than only the entry scope's project.
  const [activeScope, setActiveScope] = useState<HelperScope | undefined>(undefined);
  const status = useHelperStatus(open, activeScope);
  const conversations = useHelperConversations(open);
  const deleteConversation = useDeleteHelperConversation();

  const [conversationId, setConversationId] = useState<string | null>(null);
  const [activeTitle, setActiveTitle] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<HelperConversationSummary | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const [view, setView] = useState<'chat' | 'history'>('chat');
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [showJump, setShowJump] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const idRef = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const nextId = useCallback(() => `turn-${++idRef.current}`, []);

  // A scope that turns out to be unavailable replaces the body; announce it
  // through the panel's status region so screen-reader users hear why.
  // Adjusted during render (like the reset below) so it lands in the same pass.
  const scopeErrorMessage = status.isError ? classifyStatusError(status.error)?.message : undefined;
  const [announcedScopeError, setAnnouncedScopeError] = useState<string | undefined>(undefined);
  if (scopeErrorMessage !== announcedScopeError) {
    setAnnouncedScopeError(scopeErrorMessage);
    if (scopeErrorMessage) setAnnouncement(scopeErrorMessage);
  }

  // Each open starts a fresh (optionally scoped) conversation. A new scope
  // object only arrives via openHelper(), so this resets exactly on open and
  // when the entry point changes the scope. State is reset during render (no
  // effect cascade); the in-flight request is aborted in the effect below.
  const [resetFor, setResetFor] = useState<{ open: boolean; scope?: HelperScope }>({ open: false });
  if (resetFor.open !== open || resetFor.scope !== scope) {
    setResetFor({ open, scope });
    if (open) {
      setConversationId(null);
      setActiveTitle(null);
      setActiveScope(scope);
      setTurns([]);
      setInput('');
      setStreaming(false);
      setView('chat');
      setAnnouncement('');
      setShowJump(false);
    }
  }
  useEffect(() => {
    if (!open) return;
    // Supersede any in-flight turn so its finally block can't write stale
    // conversation/scope state back over the freshly-reset drawer.
    abortRef.current?.abort();
    abortRef.current = null;
    stickRef.current = true;
  }, [open, scope]);

  // Abort any in-flight turn when the panel finally unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Move focus to the prompt once the chat form is actually available.
  const canChat = status.data?.enabled === true && status.data.key_source != null;
  // The header keeps its controls once any status exists (even a disabled,
  // missing-key or failed scope) so the user can always switch back; only the
  // very first load, before the entry scope's status resolves, hides them.
  const hasStatus = status.data !== undefined || status.isError;
  useEffect(() => {
    if (open && canChat && view === 'chat') inputRef.current?.focus();
  }, [open, canChat, view]);

  // Follow the answer while the reader is at the bottom; once they scroll up,
  // stop following and offer a "jump to latest" pill instead.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || view !== 'chat') return;
    if (stickRef.current) el.scrollTop = el.scrollHeight;
  }, [turns, view]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX;
    stickRef.current = atBottom;
    setShowJump(!atBottom);
  };

  const jumpToLatest = () => {
    const el = scrollerRef.current;
    if (!el) return;
    stickRef.current = true;
    setShowJump(false);
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (typeof el.scrollTo === 'function') el.scrollTo({ top: el.scrollHeight, behavior: reduce ? 'auto' : 'smooth' });
    else el.scrollTop = el.scrollHeight;
  };

  /** Start a fresh thread; keeps the current scope unless one is given. */
  function newConversation(opts?: { scope: HelperScope | undefined }) {
    const nextScope = opts ? opts.scope : activeScope;
    abortRef.current?.abort();
    abortRef.current = null;
    // The aborted turn's finally skips state updates (it no longer owns
    // abortRef), so clear the streaming flag here or the composer stays locked.
    setStreaming(false);
    setConversationId(null);
    setActiveTitle(null);
    setActiveScope(nextScope);
    setTurns([]);
    setInput('');
    setView('chat');
    setAnnouncement('');
    stickRef.current = true;
    setShowJump(false);
    inputRef.current?.focus();
  }

  async function loadConversation(id: string) {
    if (!auth || streaming) return;
    setOpeningId(id);
    try {
      const conv = await auth.client.getHelperConversation(id);
      const loaded: Turn[] = [];
      // Tool rows are stored next to the answer they fed (their order within a
      // turn isn't guaranteed), so fold them into that turn's assistant.
      let pendingSteps: ToolStep[] = [];
      let lastAssistant: Turn | null = null;
      for (const m of conv.messages) {
        if (m.role === 'user') {
          loaded.push({ id: nextId(), role: 'user', content: m.content, toolSteps: [], citations: [] });
          pendingSteps = [];
          lastAssistant = null;
        } else if (m.role === 'assistant') {
          lastAssistant = {
            id: nextId(),
            role: 'assistant',
            content: m.content,
            toolSteps: pendingSteps,
            citations: m.citations ?? [],
            status: 'done',
          };
          pendingSteps = [];
          loaded.push(lastAssistant);
        } else if (m.role === 'tool') {
          const step: ToolStep = {
            id: m.tool_call_id ?? m.id,
            name: m.tool_name ?? 'tool',
            args: m.tool_args ?? {},
            status: 'ok',
            itemCount: null,
          };
          if (lastAssistant) lastAssistant.toolSteps = [...lastAssistant.toolSteps, step];
          else pendingSteps.push(step);
        }
      }
      setConversationId(id);
      setActiveTitle(conv.title);
      setActiveScope(
        conv.scope
          ? { project_id: conv.scope.project_id ?? undefined, session_ids: conv.scope.session_ids }
          : undefined,
      );
      setTurns(loaded);
      setView('chat');
      stickRef.current = true;
      setShowJump(false);
    } catch (err) {
      addToast('error', `Couldn't open conversation: ${errorMessage(err)}`);
    } finally {
      setOpeningId(null);
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  /**
   * `retry` re-asks after a failed answer: the server then reuses the stored
   * question when it is still the thread's unanswered last message, instead
   * of saving it a second time.
   */
  async function send(text?: string, opts: { retry?: boolean } = {}) {
    const question = (text ?? input).trim();
    if (!question || streaming || !auth || question.length > MAX_MESSAGE_CHARS) return;
    // Availability is still being rechecked for a new scope: suggestion chips
    // and Retry would otherwise send under the previous scope's status.
    if (status.isPlaceholderData) return;
    if (text === undefined) setInput('');

    // New conversations use the active scope; continuations re-send the scope
    // the thread was restored with (loadConversation sets activeScope from the
    // API's stored scope). The server also accepts a scope-less continuation
    // and recovers the stored scope itself.
    const sentScope = activeScope;
    const body: Record<string, unknown> = { message: question, stream: true };
    if (conversationId) body.conversation_id = conversationId;
    if (opts.retry && conversationId) body.retry = true;
    if (hasScope(sentScope)) {
      body.scope = { project_id: sentScope?.project_id, session_ids: sentScope?.session_ids };
    }

    const assistantId = nextId();
    setTurns((prev) => [
      ...prev,
      { id: nextId(), role: 'user', content: question, toolSteps: [], citations: [] },
      { id: assistantId, role: 'assistant', content: '', toolSteps: [], citations: [], question, status: 'streaming' },
    ]);
    setStreaming(true);
    setAnnouncement('');
    setView('chat');
    stickRef.current = true;
    setShowJump(false);

    const controller = new AbortController();
    abortRef.current = controller;
    const parser = new SseParser();

    const patchAssistant = (patch: (t: Turn) => Turn) => {
      setTurns((prev) => prev.map((t) => (t.id === assistantId ? patch(t) : t)));
    };

    let newConversationId: string | null = null;
    let gotDone = false;
    let streamFailed = false;
    let citationCount = 0;

    const handleEvent = (ev: SseEvent) => {
      try {
        if (ev.event === 'conversation') {
          newConversationId = (JSON.parse(ev.data) as { conversation_id?: string }).conversation_id ?? null;
        } else if (ev.event === 'text') {
          const delta = (JSON.parse(ev.data) as { delta?: string }).delta ?? '';
          if (delta) patchAssistant((t) => ({ ...t, content: t.content + delta }));
        } else if (ev.event === 'tool_call') {
          const c = JSON.parse(ev.data) as { id?: string; name?: string; args?: Record<string, unknown> };
          patchAssistant((t) => ({
            ...t,
            toolSteps: [
              ...t.toolSteps,
              { id: c.id ?? '', name: c.name ?? 'tool', args: c.args ?? {}, status: 'running', itemCount: null },
            ],
          }));
        } else if (ev.event === 'tool_result') {
          const r = JSON.parse(ev.data) as { id?: string; name?: string; ok?: boolean; item_count?: number };
          const finished: Pick<ToolStep, 'status' | 'itemCount'> = {
            status: r.ok === false ? 'error' : 'ok',
            itemCount: typeof r.item_count === 'number' ? r.item_count : null,
          };
          patchAssistant((t) => {
            const idx = r.id ? t.toolSteps.findIndex((s) => s.id === r.id && s.status === 'running') : -1;
            const toolSteps =
              idx >= 0
                ? t.toolSteps.map((s, i) => (i === idx ? { ...s, ...finished } : s))
                : [...t.toolSteps, { id: r.id ?? '', name: r.name ?? 'tool', args: {}, ...finished }];
            return { ...t, toolSteps };
          });
        } else if (ev.event === 'citation') {
          const c = JSON.parse(ev.data) as HelperCitation;
          citationCount += 1;
          patchAssistant((t) => ({
            ...t,
            citations: [...t.citations, { session_id: c.session_id, message_index: c.message_index ?? null, title: c.title }],
          }));
        } else if (ev.event === 'error') {
          const e = JSON.parse(ev.data) as { code?: string; message?: string };
          streamFailed = true;
          patchAssistant((t) => ({ ...t, error: streamError(e.code, e.message) }));
        } else if (ev.event === 'done') {
          gotDone = true;
        }
      } catch {
        // A malformed frame should never surface — ignore it and keep the rest.
      }
    };

    const process = (chunk: string) => {
      for (const ev of parser.push(chunk)) handleEvent(ev);
    };

    let outcome: Turn['status'] = 'done';
    let failure: HelperError | null = null;
    try {
      const resp = await fetch(`${auth.baseUrl}/api/v1/helper/turns`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${auth.apiKey}`,
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!resp.ok) throw new ApiError(resp.status, await resp.text());
      if (!resp.body) throw new Error('The helper returned an empty response.');

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        process(decoder.decode(value, { stream: true }));
      }
      process(decoder.decode()); // flush any trailing bytes
      for (const ev of parser.flush()) handleEvent(ev); // final frame with no blank line

      if (streamFailed) outcome = 'error';
      else if (!gotDone) {
        outcome = 'error';
        failure = { kind: 'disconnected', message: 'The connection closed before the answer finished.' };
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        // User pressed Stop; the server persists the partial answer.
        outcome = 'stopped';
      } else {
        outcome = 'error';
        failure = classifyError(err);
      }
    } finally {
      // Only the turn that still owns the abort ref may touch state after an
      // await: a reset (open/new) nulls abortRef so a stale aborted turn can't
      // write its error back into the freshly-opened conversation.
      if (abortRef.current === controller) {
        const settledStatus = outcome;
        const err = failure;
        patchAssistant((t) => ({
          ...t,
          status: settledStatus,
          error: t.error ?? err ?? undefined,
          // Steps still marked running when the stream ends didn't report back.
          toolSteps: t.toolSteps.map((s) =>
            s.status === 'running' ? { ...s, status: settledStatus === 'error' ? 'error' : 'ok' } : s,
          ),
        }));
        setStreaming(false);
        abortRef.current = null;
        if (settledStatus === 'done') {
          setAnnouncement(
            citationCount > 0
              ? `Answer complete, with ${citationCount} ${citationCount === 1 ? 'source' : 'sources'}.`
              : 'Answer complete.',
          );
        } else if (settledStatus === 'stopped') {
          setAnnouncement('Answer stopped.');
        }
        if (err && (err.kind === 'key_required' || err.kind === 'upgrade_required')) void status.refetch();
        if (newConversationId) {
          setConversationId(newConversationId);
          void conversations.refetch();
        }
      }
    }
  }

  function retry(assistantId: string) {
    const idx = turns.findIndex((t) => t.id === assistantId);
    const question = turns[idx]?.question;
    if (idx < 0 || !question || streaming || status.isPlaceholderData) return;
    // Drop the failed exchange (question + error) and ask again.
    setTurns((prev) => prev.filter((_, i) => i !== idx && !(i === idx - 1 && prev[i].role === 'user')));
    void send(question, { retry: true });
  }

  function changeScope(option: ScopeOption) {
    if (turns.length > 0 || conversationId) newConversation({ scope: option.scope });
    else setActiveScope(option.scope);
    inputRef.current?.focus();
  }

  // ── Derived display state ──

  const options = useMemo(() => scopeOptions(scope, activeScope), [scope, activeScope]);
  const current = useMemo(() => activeOption(scope, activeScope), [scope, activeScope]);
  const isOrgMember = !!me.data?.org_id && me.data?.org_role === 'member';
  const isOrgAdmin = !!me.data?.org_id && (me.data?.org_role === 'admin' || me.data?.org_role === 'owner');

  const keyHint: ReactNode = isOrgMember ? (
    <span className="mt-1 block">Or ask your org admin to add an org key.</span>
  ) : null;

  const modelLabel = useMemo(() => {
    const d = status.data;
    if (!d) return null;
    return [keyLabel(d.key_source), d.provider, d.model].filter(Boolean).join(' · ') || null;
  }, [status.data]);

  const placeholder =
    current.kind === 'project'
      ? `Ask about ${current.label === 'This project' ? 'this project' : current.label}…`
      : current.kind === 'session'
        ? 'Ask about this session…'
        : 'Ask about your sessions…';

  // ── Body ──

  const statusScopeError = status.isError ? classifyStatusError(status.error) : null;

  let body: ReactNode;
  if (status.isLoading && !status.data) {
    body = (
      <div className="flex-1 space-y-3 px-5 py-8" aria-busy="true" aria-label="Loading the helper">
        <div className="h-10 w-10 animate-skeleton-pulse rounded-xl bg-surface-hover" />
        <div className="h-4 w-2/3 animate-skeleton-pulse rounded bg-surface-hover" />
        <div className="h-3 w-full animate-skeleton-pulse rounded bg-surface-hover" />
        <div className="h-3 w-5/6 animate-skeleton-pulse rounded bg-surface-hover" />
      </div>
    );
  } else if (statusScopeError) {
    body = (
      <HelperNotice
        tone="warning"
        icon={<AlertIcon size={20} />}
        title="This scope is unavailable"
        actions={
          scopeKey(activeScope) === scopeKey(scope) ? (
            // The entry scope itself is gone (e.g. project access revoked):
            // restarting there would hit the same error.
            <Button onClick={() => newConversation({ scope: undefined })}>Ask about all my sessions</Button>
          ) : (
            <Button onClick={() => newConversation({ scope })}>Start a new conversation</Button>
          )
        }
      >
        <p>{statusScopeError.message}</p>
      </HelperNotice>
    );
  } else if (status.isError) {
    body = (
      <HelperNotice
        tone="danger"
        icon={<AlertIcon size={20} />}
        title="Couldn't load the helper"
        actions={
          <Button variant="secondary" onClick={() => void status.refetch()}>
            Retry
          </Button>
        }
      >
        <p>We couldn't check whether the session helper is available for your account.</p>
      </HelperNotice>
    );
  } else if (status.data && !status.data.enabled) {
    body = (
      <HelperNotice
        icon={<LockIcon size={20} />}
        title="An Enterprise feature"
        actions={
          <Link to="/settings/billing" onClick={onClose} className={linkButtonPrimary}>
            View plans
          </Link>
        }
      >
        <p>
          The session helper answers questions across your sessions and project knowledge, with links to every
          source. It is available on the Enterprise plan.
        </p>
      </HelperNotice>
    );
  } else if (status.data && status.data.key_source === null) {
    body = (
      <HelperNotice
        icon={<KeyIcon size={20} />}
        title="Connect a model to get started"
        actions={
          <>
            <Link to="/settings/data-protection" onClick={onClose} className={linkButtonPrimary}>
              Open Data protection settings
            </Link>
            {isOrgAdmin && (
              <Link to="/settings/organization?tab=policies" onClick={onClose} className={linkButtonSecondary}>
                Set an org key
              </Link>
            )}
          </>
        }
      >
        <p>
          The helper runs on your own model key — SessionFS never supplies one. Add a key in{' '}
          <span className="font-medium text-text-primary">Settings → Data protection → Security review model</span>{' '}
          to use the session helper.
        </p>
        {isOrgMember && <p>Or ask your org admin to add an org key.</p>}
        {isOrgAdmin && <p>As an org admin you can also add one key for everyone in your organization.</p>}
      </HelperNotice>
    );
  } else if (view === 'history') {
    body = (
      <div className="min-h-0 flex-1 overflow-y-auto">
        <HelperHistory
          conversations={conversations.data}
          loading={conversations.isLoading}
          error={conversations.isError === true}
          onRetry={() => void conversations.refetch()}
          activeId={conversationId}
          openingId={openingId}
          busy={streaming}
          orgId={me.data?.org_id ?? null}
          orgName={me.data?.org_name ?? null}
          onOpen={(id) => void loadConversation(id)}
          onDelete={setDeleteTarget}
          onNew={() => newConversation()}
        />
      </div>
    );
  } else {
    body = (
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div ref={scrollerRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {turns.length === 0 ? (
            <HelperEmptyState
              scope={current}
              onPick={(p) => void send(p)}
              disabled={status.isPlaceholderData === true}
              recent={conversations.data ?? []}
              onOpenConversation={(id) => void loadConversation(id)}
              onShowHistory={() => setView('history')}
            />
          ) : (
            <section aria-label="Conversation" aria-busy={streaming} className="space-y-6 px-4 py-5 sm:px-5">
              {turns.map((t) =>
                t.role === 'user' ? (
                  <UserMessage key={t.id} content={t.content} />
                ) : (
                  <AssistantMessage
                    key={t.id}
                    turn={t}
                    actions={{
                      onRetry: t.question ? () => retry(t.id) : undefined,
                      onNewConversation: () => newConversation(),
                      onNavigate: onClose,
                      keyHint,
                    }}
                  />
                ),
              )}
            </section>
          )}
        </div>
        {showJump && turns.length > 0 && (
          <button
            type="button"
            onClick={jumpToLatest}
            className="absolute bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-overlay px-3 py-1.5 text-xs font-medium text-text-primary transition-colors hover:bg-bg-elevated outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            style={{ boxShadow: 'var(--shadow-overlay)' }}
          >
            <ArrowDownIcon size={13} />
            Jump to latest
          </button>
        )}
      </div>
    );
  }

  return (
    <Drawer open={open} onClose={onClose} titleId={TITLE_ID} bare widthClassName="w-full sm:w-[480px] lg:w-[520px]">
      <div className="flex h-full min-h-0 flex-col bg-surface">
        <HelperHeader
          options={options}
          active={current}
          onScopeChange={changeScope}
          threadTitle={activeTitle}
          hasThread={turns.length > 0 || conversationId !== null}
          view={view}
          onToggleHistory={() => setView((v) => (v === 'history' ? 'chat' : 'history'))}
          onNew={() => newConversation()}
          onClose={onClose}
          showControls={hasStatus}
        />

        {body}

        {canChat && view === 'chat' && (
          <HelperComposer
            ref={inputRef}
            value={input}
            onChange={setInput}
            onSend={() => void send()}
            onStop={stop}
            streaming={streaming}
            placeholder={placeholder}
            modelLabel={modelLabel}
            checking={status.isPlaceholderData === true}
          />
        )}

        {/* Announces each finished answer once — never the individual tokens. */}
        <div className="sr-only" role="status" aria-live="polite">
          {announcement}
        </div>
      </div>

      <Dialog open={deleteTarget !== null} onClose={() => setDeleteTarget(null)} titleId="helper-delete-dialog-title">
        <DialogHeader titleId="helper-delete-dialog-title">Delete conversation?</DialogHeader>
        <p className="text-sm text-text-secondary">
          This permanently deletes “{deleteTarget?.title || 'this conversation'}” and its messages.
        </p>
        <DialogFooter>
          <Button variant="secondary" onClick={() => setDeleteTarget(null)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={deleteConversation.isPending}
            onClick={() => {
              if (!deleteTarget) return;
              deleteConversation.mutate(deleteTarget.id, {
                onSuccess: () => {
                  addToast('success', 'Conversation deleted');
                  if (deleteTarget.id === conversationId) newConversation();
                  setDeleteTarget(null);
                },
                onError: (err) => addToast('error', `Delete failed: ${errorMessage(err)}`),
              });
            }}
          >
            Delete
          </Button>
        </DialogFooter>
      </Dialog>
    </Drawer>
  );
}
