/**
 * The session-helper drawer: the Enterprise "ask over your sessions" panel.
 *
 * Streaming is done with raw `fetch` + `ReadableStream` (EventSource can't set
 * the `Authorization` header), the chunks are framed by `SseParser`, and the
 * text deltas are rendered progressively through the shared `ui/Markdown`.
 * Tool steps and citations are folded into the assistant turn as they arrive.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext';
import { ApiError, type HelperCitation, type HelperConversationSummary } from '../api/client';
import { useMe } from '../hooks/useMe';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { Button, Dialog, DialogFooter, DialogHeader, Drawer, Markdown } from '../components/ui';
import type { HelperScope } from './HelperProvider';
import { useDeleteHelperConversation, useHelperConversations, useHelperStatus } from './useHelper';
import { SseParser, type SseEvent } from './sse';

interface ToolStep {
  id: string;
  name: string;
  ok: boolean;
  itemCount: number;
}

interface Turn {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  toolSteps: ToolStep[];
  citations: HelperCitation[];
  error?: string;
}

// The server stores each thread's last scope and returns it from the
// conversation endpoint, so resuming a scoped thread no longer needs a
// sessionStorage sidecar — loadConversation restores the scope from the API.
function hasScope(scope?: HelperScope): boolean {
  return !!scope && (!!scope.project_id || (scope.session_ids?.length ?? 0) > 0);
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
  const status = useHelperStatus(open);
  const conversations = useHelperConversations(open);
  const deleteConversation = useDeleteHelperConversation();

  const [conversationId, setConversationId] = useState<string | null>(null);
  const [activeTitle, setActiveTitle] = useState<string | null>(null);
  const [activeScope, setActiveScope] = useState<HelperScope | undefined>(undefined);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<HelperConversationSummary | null>(null);
  const [doneAnnouncement, setDoneAnnouncement] = useState('');

  const abortRef = useRef<AbortController | null>(null);
  const idRef = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const nextId = useCallback(() => `turn-${++idRef.current}`, []);

  // Each open starts a fresh (optionally scoped) conversation. A new scope
  // object only arrives via openHelper(), so [open, scope] re-runs exactly on
  // open and when the entry point changes the scope.
  useEffect(() => {
    if (!open) return;
    abortRef.current?.abort();
    // Supersede any in-flight turn so its finally block can't write stale
    // conversation/scope state back over the freshly-reset drawer.
    abortRef.current = null;
    setConversationId(null);
    setActiveTitle(null);
    setActiveScope(scope);
    setTurns([]);
    setInput('');
    setStreaming(false);
  }, [open, scope]);

  // Abort any in-flight turn when the panel finally unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Move focus to the prompt once the chat form is actually available.
  const canChat = status.data?.enabled === true && status.data.key_source != null;
  useEffect(() => {
    if (open && canChat) inputRef.current?.focus();
  }, [open, canChat]);

  // Auto-scroll the streaming answer into view as deltas arrive.
  const transcriptRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (streaming) transcriptRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }, [turns, streaming]);

  function newConversation() {
    abortRef.current?.abort();
    abortRef.current = null;
    // The aborted turn's finally skips state updates (it no longer owns
    // abortRef), so clear the streaming flag here or the composer stays locked.
    setStreaming(false);
    setConversationId(null);
    setActiveTitle(null);
    setActiveScope(undefined);
    setTurns([]);
    setInput('');
  }

  async function loadConversation(id: string) {
    if (!auth || streaming) return;
    try {
      const conv = await auth.client.getHelperConversation(id);
      const loaded: Turn[] = [];
      for (const m of conv.messages) {
        if (m.role === 'user') {
          loaded.push({ id: nextId(), role: 'user', content: m.content, toolSteps: [], citations: [] });
        } else if (m.role === 'assistant') {
          loaded.push({
            id: nextId(),
            role: 'assistant',
            content: m.content,
            toolSteps: [],
            citations: m.citations ?? [],
          });
        }
        // role === 'tool' rows are intermediate steps; the answer already folds them in.
      }
      setConversationId(id);
      setActiveTitle(conv.title);
      setActiveScope(
        conv.scope
          ? { project_id: conv.scope.project_id ?? undefined, session_ids: conv.scope.session_ids }
          : undefined,
      );
      setTurns(loaded);
    } catch (err) {
      addToast('error', `Couldn't open conversation: ${errorMessage(err)}`);
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  async function send() {
    const question = input.trim();
    if (!question || streaming || !auth) return;
    setInput('');

    // New conversations use the active scope; continuations re-send the scope
    // the thread was restored with (loadConversation sets activeScope from the
    // API's stored scope). The server also accepts a scope-less continuation
    // and recovers the stored scope itself.
    const sentScope = activeScope;
    const body: Record<string, unknown> = { message: question, stream: true };
    if (conversationId) body.conversation_id = conversationId;
    if (hasScope(sentScope)) {
      body.scope = { project_id: sentScope?.project_id, session_ids: sentScope?.session_ids };
    }

    const assistantId = nextId();
    setTurns((prev) => [
      ...prev,
      { id: nextId(), role: 'user', content: question, toolSteps: [], citations: [] },
      { id: assistantId, role: 'assistant', content: '', toolSteps: [], citations: [] },
    ]);
    setStreaming(true);
    setDoneAnnouncement('');

    const controller = new AbortController();
    abortRef.current = controller;
    const parser = new SseParser();

    const patchAssistant = (patch: (t: Turn) => Turn) => {
      setTurns((prev) => prev.map((t) => (t.id === assistantId ? patch(t) : t)));
    };

    let newConversationId: string | null = null;

    const handleEvent = (ev: SseEvent) => {
      try {
        if (ev.event === 'conversation') {
          newConversationId = (JSON.parse(ev.data) as { conversation_id?: string }).conversation_id ?? null;
        } else if (ev.event === 'text') {
          const delta = (JSON.parse(ev.data) as { delta?: string }).delta ?? '';
          if (delta) patchAssistant((t) => ({ ...t, content: t.content + delta }));
        } else if (ev.event === 'tool_result') {
          const r = JSON.parse(ev.data) as { id?: string; name?: string; ok?: boolean; item_count?: number };
          patchAssistant((t) => ({
            ...t,
            toolSteps: [
              ...t.toolSteps,
              { id: r.id ?? '', name: r.name ?? 'tool', ok: r.ok !== false, itemCount: r.item_count ?? 0 },
            ],
          }));
        } else if (ev.event === 'citation') {
          const c = JSON.parse(ev.data) as HelperCitation;
          patchAssistant((t) => ({
            ...t,
            citations: [...t.citations, { session_id: c.session_id, message_index: c.message_index ?? null, title: c.title }],
          }));
        } else if (ev.event === 'error') {
          const message = (JSON.parse(ev.data) as { message?: string }).message;
          patchAssistant((t) => ({ ...t, error: message ?? 'The helper failed to answer.' }));
        } else if (ev.event === 'done') {
          setDoneAnnouncement('Answer complete');
        }
        // tool_call (running state) carries no display state.
      } catch {
        // A malformed frame should never surface — ignore it and keep the rest.
      }
    };

    const process = (chunk: string) => {
      for (const ev of parser.push(chunk)) handleEvent(ev);
    };

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
    } catch (err) {
      // Only the turn that still owns the abort ref may touch state after an
      // await: a reset (open/new) nulls abortRef so a stale aborted turn can't
      // write its error back into the freshly-opened conversation.
      if (abortRef.current === controller && (err as Error).name !== 'AbortError') {
        // AbortError = user pressed Stop; the server persists the partial answer.
        patchAssistant((t) => ({ ...t, error: t.error ?? errorMessage(err) }));
      }
    } finally {
      if (abortRef.current !== controller) return;
      setStreaming(false);
      abortRef.current = null;
      if (newConversationId) {
        setConversationId(newConversationId);
        void conversations.refetch();
      }
    }
  }

  // ── Gate states ──

  let body: ReactNode;
  if (status.isLoading) {
    body = <p className="text-sm text-text-tertiary py-4">Loading…</p>;
  } else if (status.isError) {
    body = (
      <div className="text-sm text-text-secondary">
        <p className="text-danger">Couldn't check the helper status.</p>
        <Button variant="secondary" size="sm" className="mt-3" onClick={() => void status.refetch()}>
          Retry
        </Button>
      </div>
    );
  } else if (status.data && !status.data.enabled) {
    body = (
      <div className="text-sm text-text-secondary">
        <p>The session helper is available on the Enterprise plan.</p>
        <Link to="/settings/billing" className="text-brand hover:underline mt-1 inline-block">
          View plans
        </Link>
      </div>
    );
  } else if (status.data && status.data.key_source === null) {
    const isOrgMember = !!me.data?.org_id && me.data?.org_role === 'member';
    body = (
      <div className="text-sm text-text-secondary">
        <p>Add a key in Settings → Data protection → Security review model to use the session helper.</p>
        {isOrgMember && <p className="mt-1">Or ask your org admin to add an org key.</p>}
      </div>
    );
  } else {
    body = (
      <div className="flex flex-col gap-3">
        {/* Scope / conversation context */}
        {(activeScope || activeTitle) && (
          <div className="text-2xs text-text-tertiary">
            {activeScope?.project_id
              ? 'About this project'
              : activeScope?.session_ids?.length
                ? 'About this session'
                : activeTitle}
          </div>
        )}

        {/* History + New stay available even while a conversation is open, so
            the user can switch threads or start fresh without closing first. */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-micro uppercase font-semibold tracking-wide text-text-tertiary">
              Conversations
            </span>
            <Button variant="ghost" size="sm" onClick={newConversation}>
              New
            </Button>
          </div>
          {conversations.data && conversations.data.length === 0 ? (
            <p className="text-xs text-text-tertiary">No conversations yet.</p>
          ) : (
            <ul className="max-h-52 overflow-y-auto space-y-0.5" aria-label="Conversation history">
              {conversations.data?.map((c) => (
                <li key={c.id} className="group flex items-center gap-1 rounded-md hover:bg-surface-hover">
                  <button
                    type="button"
                    disabled={streaming}
                    onClick={() => void loadConversation(c.id)}
                    className="flex-1 min-w-0 text-left px-2 py-1.5 text-sm text-text-secondary hover:text-text-primary truncate outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-md"
                  >
                    {c.title || 'Untitled conversation'}
                  </button>
                  <button
                    type="button"
                    disabled={streaming}
                    onClick={() => setDeleteTarget(c)}
                    aria-label={`Delete ${c.title || 'Untitled conversation'}`}
                    className="shrink-0 p-1.5 text-text-tertiary hover:text-danger rounded-md outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                      <path d="M10 11v6M14 11v6" />
                      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                    </svg>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Transcript */}
        <div ref={transcriptRef} className="space-y-4" aria-live="polite" aria-busy={streaming}>
          {turns.map((t) =>
            t.role === 'user' ? (
              <div key={t.id} className="text-sm">
                <div className="text-2xs uppercase tracking-wide text-text-tertiary mb-1">You</div>
                <p className="text-text-primary whitespace-pre-wrap">{t.content}</p>
              </div>
            ) : (
              <div key={t.id} className="text-sm">
                <div className="text-2xs uppercase tracking-wide text-text-tertiary mb-1">Helper</div>

                {t.toolSteps.length > 0 && (
                  <ul className="mb-2 space-y-1">
                    {t.toolSteps.map((s, i) => (
                      <li key={`${s.id}-${i}`} className="flex items-center gap-1.5 text-xs text-text-tertiary">
                        <span aria-hidden="true">{s.ok ? '✓' : '✗'}</span>
                        <span className="font-mono">{s.name}</span>
                        <span>· {s.itemCount} {s.itemCount === 1 ? 'item' : 'items'}</span>
                      </li>
                    ))}
                  </ul>
                )}

                {t.content ? (
                  <Markdown>{t.content}</Markdown>
                ) : (
                  t.error === undefined && <p className="text-text-tertiary">Thinking…</p>
                )}

                {t.citations.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {t.citations.map((c, i) => (
                      // The message_index maps to the raw messages.jsonl line, not
                      // the dashboard's paginated render, so there is no matching
                      // #msg-N anchor to link to. Link to the session only, and
                      // close the drawer before navigating away.
                      <Link
                        key={`${c.session_id}-${i}`}
                        to={`/sessions/${c.session_id}`}
                        onClick={onClose}
                        className="text-xs text-brand underline underline-offset-2 decoration-brand/40 hover:decoration-brand"
                      >
                        {c.title || c.session_id}
                      </Link>
                    ))}
                  </div>
                )}

                {t.error && <p className="text-danger text-xs mt-2">{t.error}</p>}
              </div>
            ),
          )}
        </div>

        {/* Screen-reader announcement that a turn finished streaming. */}
        <div className="sr-only" role="status">
          {doneAnnouncement}
        </div>

        {/* Composer */}
        <div className="flex items-center gap-2 pt-1">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            disabled={streaming}
            placeholder="Ask about your sessions…"
            aria-label="Ask the session helper"
            className="flex-1 min-w-0 px-3 py-2 text-sm bg-bg-sunken border border-border rounded-lg text-text-primary placeholder:text-text-tertiary outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50"
          />
          {streaming ? (
            <Button variant="secondary" onClick={stop}>
              Stop
            </Button>
          ) : (
            <Button onClick={() => void send()} disabled={!input.trim()}>
              Send
            </Button>
          )}
        </div>
      </div>
    );
  }

  return (
    <Drawer open={open} onClose={onClose} titleId="helper-panel-title">
      <div className="flex flex-col gap-4">
        <h2 id="helper-panel-title" className="text-lg font-semibold text-text-primary">
          Session helper
        </h2>
        {body}

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
      </div>
    </Drawer>
  );
}
