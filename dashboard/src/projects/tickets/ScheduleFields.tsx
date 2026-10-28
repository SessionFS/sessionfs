import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react';
import type { Ticket } from '../../api/client';
import { useUpdateTicket } from '../../hooks/useTickets';
import { Badge } from '../../components/Badge';
import { Button, Input } from '../../components/ui';
import { ReapprovalConfirm } from './ReapprovalConfirm';
import { estimateError, formatDay, formatDueDate, isOverdue, scheduleErrorMessage } from './schedule';

/* ── Ticket facts: who filed / resolved it, due date and estimate ──
 *
 * The due date and estimate edit in place for whoever may update the ticket
 * (its creator or a project admin — update_ticket's rule). An explicit null
 * clears a value; the server validates the estimate again and its 422
 * message is shown under the field. A non-editor's change to an approved
 * (open) ticket sends it back for approval, so that save asks first. */

interface Props {
  projectId: string;
  ticket: Ticket;
  canEdit: boolean;
  /** Saving withdraws the ticket's approval (non-editor, status open). */
  needsReapproval?: boolean;
}

export function TicketFacts({ projectId, ticket: t, canEdit, needsReapproval = false }: Props) {
  const creator = t.created_by;
  const creatorName =
    creator?.display_name?.trim() || creator?.persona?.trim() || t.created_by_persona?.trim() || 'Unknown';
  const creatorPersona =
    creator?.display_name?.trim() && (creator.persona ?? t.created_by_persona)?.trim();
  const resolver = t.resolved_by;
  const resolverName = resolver ? resolver.display_name?.trim() || resolver.email || 'Unknown' : null;

  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 rounded-lg border border-border bg-surface px-3 py-2.5 text-xs sm:grid-cols-2">
      <Fact label="Created">
        <span className="font-medium text-text-primary">{creatorName}</span>
        {creatorPersona && <span className="text-text-tertiary"> ({creatorPersona})</span>}
        {creator?.email && (
          <span className="break-words text-text-tertiary"> · {creator.email}</span>
        )}
        {t.created_at && <span className="text-text-tertiary"> · {formatDay(t.created_at)}</span>}
      </Fact>
      {resolver && (
        <Fact label="Resolved">
          by <span className="font-medium text-text-primary">{resolverName}</span>
          {resolver.display_name && resolver.email && (
            <span className="break-words text-text-tertiary"> · {resolver.email}</span>
          )}
          {t.resolved_at && <span className="text-text-tertiary"> on {formatDay(t.resolved_at)}</span>}
        </Fact>
      )}
      <DueDateFact projectId={projectId} ticket={t} canEdit={canEdit} needsReapproval={needsReapproval} />
      <EstimateFact projectId={projectId} ticket={t} canEdit={canEdit} needsReapproval={needsReapproval} />
    </dl>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 items-baseline gap-2">
      <dt className="w-16 shrink-0 text-text-tertiary">{label}</dt>
      <dd className="min-w-0 flex-1 text-text-secondary">{children}</dd>
    </div>
  );
}

type ScheduleField = 'due_date' | 'estimated_effort';

/** Editing state and the save path shared by both fields. Focus moves into
 *  the editor on open and back to its trigger on close; Escape is claimed
 *  (so a surrounding drawer stays open) and ignored while a save is in
 *  flight; a save that would withdraw approval waits for a confirm. */
function useScheduleEditor(
  projectId: string,
  ticket: Ticket,
  field: ScheduleField,
  needsReapproval: boolean,
) {
  const update = useUpdateTicket(projectId);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | undefined>();
  // A value waiting for the "sends it back for approval" confirm.
  const [confirming, setConfirming] = useState<{ value: string | null } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const inputId = useId();
  const wasEditing = useRef(false);
  useEffect(() => {
    if (wasEditing.current && !editing) triggerRef.current?.focus();
    wasEditing.current = editing;
  }, [editing]);
  // Leaving the confirm (Keep editing, or a refused save) hands focus back
  // to the field; the confirm's own button took it on the way in.
  const wasConfirming = useRef(false);
  useEffect(() => {
    if (wasConfirming.current && !confirming && editing) document.getElementById(inputId)?.focus();
    wasConfirming.current = !!confirming;
  }, [confirming, editing, inputId]);

  function close() {
    setError(undefined);
    setConfirming(null);
    setEditing(false);
  }

  async function commit(value: string | null) {
    setError(undefined);
    try {
      await update.mutateAsync({ ticketId: ticket.id, body: { [field]: value } });
      close();
    } catch (exc) {
      setConfirming(null);
      setError(scheduleErrorMessage(exc));
    }
  }

  return {
    editing,
    error,
    setError,
    confirming,
    pending: update.isPending,
    triggerRef,
    inputId,
    open: () => {
      setError(undefined);
      setConfirming(null);
      setEditing(true);
    },
    close,
    /** Save, or first ask when saving withdraws approval. */
    save: (value: string | null) => {
      if (needsReapproval && !confirming) {
        setConfirming({ value });
        return;
      }
      void commit(value);
    },
    confirm: () => {
      if (confirming) void commit(confirming.value);
    },
    cancelConfirm: () => setConfirming(null),
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      if (!update.isPending) close();
    },
  };
}

type Editor = ReturnType<typeof useScheduleEditor>;

function EditTrigger({
  label,
  hasValue,
  onClick,
  triggerRef,
}: {
  label: string;
  hasValue: boolean;
  onClick: () => void;
  triggerRef: React.RefObject<HTMLButtonElement | null>;
}) {
  return (
    <Button
      ref={triggerRef}
      variant="ghost"
      size="sm"
      className="!ml-1 !px-1.5 !py-0.5 !text-xs !font-normal !text-brand"
      aria-label={`${hasValue ? 'Edit' : 'Set'} ${label}`}
      onClick={onClick}
    >
      {hasValue ? 'Edit' : 'Set'}
    </Button>
  );
}

/** The editing row: the control, then Save / Clear / Cancel, or the
 *  approval confirm in their place. */
function EditorForm({
  label,
  ed,
  hasValue,
  onSubmit,
  children,
}: {
  label: string;
  ed: Editor;
  hasValue: boolean;
  onSubmit: (e: FormEvent) => void;
  children: ReactNode;
}) {
  return (
    // Escape is handled on the whole group so it also covers the confirm.
    <div role="group" aria-label={`Edit ${label}`} onKeyDown={ed.onKeyDown}>
      <form onSubmit={onSubmit} className="flex flex-wrap items-start gap-1.5" noValidate>
        {children}
        {!ed.confirming && (
          <>
            <Button type="submit" variant="primary" size="sm" loading={ed.pending}>
              Save
            </Button>
            {hasValue && (
              <Button type="button" variant="ghost" size="sm" disabled={ed.pending} onClick={() => ed.save(null)}>
                Clear
              </Button>
            )}
            <Button type="button" variant="ghost" size="sm" disabled={ed.pending} onClick={ed.close}>
              Cancel
            </Button>
          </>
        )}
      </form>
      {ed.confirming && (
        <ReapprovalConfirm onConfirm={ed.confirm} onCancel={ed.cancelConfirm} pending={ed.pending} />
      )}
    </div>
  );
}

function DueDateFact({ projectId, ticket: t, canEdit, needsReapproval = false }: Props) {
  const ed = useScheduleEditor(projectId, t, 'due_date', needsReapproval);
  const [value, setValue] = useState(t.due_date ?? '');
  const overdue = isOverdue(t);

  function submit(e: FormEvent) {
    e.preventDefault();
    // The confirm owns the pending value (it may be a Clear).
    if (ed.confirming) return;
    if (!value) {
      ed.setError('Pick a date, or use Clear to remove it.');
      return;
    }
    ed.save(value);
  }

  if (ed.editing) {
    return (
      <Fact label="Due">
        <EditorForm label="due date" ed={ed} hasValue={!!t.due_date} onSubmit={submit}>
          <div className="w-40">
            <Input
              id={ed.inputId}
              type="date"
              aria-label="Due date"
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                ed.setError(undefined);
              }}
              readOnly={!!ed.confirming || ed.pending}
              error={ed.error}
              autoFocus
              className="!h-8 !py-1 text-xs"
            />
          </div>
        </EditorForm>
      </Fact>
    );
  }

  return (
    <Fact label="Due">
      {t.due_date ? (
        <span className={overdue ? 'font-medium text-danger' : 'text-text-primary'}>
          {formatDueDate(t.due_date)}
        </span>
      ) : (
        <span className="text-text-tertiary">No due date</span>
      )}
      {overdue && (
        <span className="ml-1.5 inline-block align-middle">
          <Badge variant="danger" tint label="Overdue" size="sm" />
        </span>
      )}
      {canEdit && (
        <EditTrigger
          label="due date"
          hasValue={!!t.due_date}
          triggerRef={ed.triggerRef}
          onClick={() => {
            setValue(t.due_date ?? '');
            ed.open();
          }}
        />
      )}
    </Fact>
  );
}

function EstimateFact({ projectId, ticket: t, canEdit, needsReapproval = false }: Props) {
  const ed = useScheduleEditor(projectId, t, 'estimated_effort', needsReapproval);
  const [value, setValue] = useState(t.estimated_effort ?? '');

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ed.confirming) return;
    const problem = estimateError(value);
    if (problem) {
      ed.setError(problem);
      return;
    }
    ed.save(value.trim());
  }

  if (ed.editing) {
    return (
      <Fact label="Estimate">
        <EditorForm label="estimate" ed={ed} hasValue={!!t.estimated_effort} onSubmit={submit}>
          <div className="w-32">
            <Input
              id={ed.inputId}
              type="text"
              aria-label="Estimate"
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                ed.setError(undefined);
              }}
              readOnly={!!ed.confirming || ed.pending}
              placeholder="2d 4h"
              maxLength={64}
              error={ed.error}
              autoFocus
              className="!h-8 !py-1 text-xs"
            />
          </div>
        </EditorForm>
      </Fact>
    );
  }

  return (
    <Fact label="Estimate">
      {t.estimated_effort ? (
        <span className="font-mono text-text-primary">{t.estimated_effort}</span>
      ) : (
        <span className="text-text-tertiary">No estimate</span>
      )}
      {canEdit && (
        <EditTrigger
          label="estimate"
          hasValue={!!t.estimated_effort}
          triggerRef={ed.triggerRef}
          onClick={() => {
            setValue(t.estimated_effort ?? '');
            ed.open();
          }}
        />
      )}
    </Fact>
  );
}
