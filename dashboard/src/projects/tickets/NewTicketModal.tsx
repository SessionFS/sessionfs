import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import type { Ticket } from '../../api/client';
import { useToast } from '../../hooks/useToast';
import { useCreateTicket, useTickets } from '../../hooks/useTickets';
import { errorMessage } from '../../utils/errorMessage';
import { Button, Dialog, DialogFooter, Input, Kbd, Select, Textarea } from '../../components/ui';
import { AssigneePicker } from './AssigneePicker';
import { UNASSIGNED, assigneeErrorFrom, assigneePayload, type Assignee } from './assignee';
import { estimateError, scheduleErrorMessage, scheduleFieldErrors } from './schedule';

/* ── New ticket ──
 *
 * Title first (focused), then the description. Kind is a two-way switch;
 * "Part of Issue" only applies to Tasks. Priority and assignee follow;
 * acceptance criteria, due date and estimate sit behind "More details"
 * (opened again when the server rejects one of them). ⌘↵ / Ctrl+↵ creates.
 * The created ticket is handed back so the page can open it. */

interface NewModalProps {
  projectId: string;
  onClose: () => void;
  /** The project's org when the viewer is a member: offers its contacts. */
  orgId?: string | null;
  /** Start as a Task under this Issue (Add child task in the ticket pane). */
  defaultParentId?: string;
  /** Called with the new ticket after it is created. */
  onCreated?: (ticket: Ticket) => void;
}

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

export function NewTicketModal({ projectId, onClose, orgId, defaultParentId, onCreated }: NewModalProps) {
  const create = useCreateTicket(projectId);
  const { addToast } = useToast();
  const formRef = useRef<HTMLFormElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [criteria, setCriteria] = useState('');
  const [priority, setPriority] = useState<'low' | 'medium' | 'high' | 'critical'>('medium');
  const [assignee, setAssignee] = useState<Assignee>(UNASSIGNED);
  const [assigneeError, setAssigneeError] = useState<string | undefined>();
  const [kind, setKind] = useState<'task' | 'issue'>('task');
  const [parentTicketId, setParentTicketId] = useState<string>(defaultParentId ?? '');
  const [dueDate, setDueDate] = useState('');
  const [estimate, setEstimate] = useState('');
  const [estimateProblem, setEstimateProblem] = useState<string | undefined>();
  const [dueDateProblem, setDueDateProblem] = useState<string | undefined>();
  const [moreOpen, setMoreOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // Open + in_progress Issues are valid parents.
  const { data: openIssues } = useTickets(projectId, { kind: 'issue', status: 'open' });
  const { data: activeIssues } = useTickets(projectId, { kind: 'issue', status: 'in_progress' });
  const issueOptions = [...(openIssues ?? []), ...(activeIssues ?? [])];

  // An Issue never sits under another Issue.
  function chooseKind(next: 'task' | 'issue') {
    setKind(next);
    if (next === 'issue') setParentTicketId('');
  }

  // Title first. After the dialog's focus trap has placed focus.
  useEffect(() => {
    const id = requestAnimationFrame(() => titleRef.current?.focus());
    titleRef.current?.focus();
    return () => cancelAnimationFrame(id);
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    // Both scheduling fields are optional; a typed estimate must parse.
    const estimateValue = estimate.trim();
    const problem = estimateValue ? estimateError(estimateValue) : null;
    if (problem) {
      setEstimateProblem(problem);
      setMoreOpen(true);
      return;
    }
    setSubmitting(true);
    setAssigneeError(undefined);
    setEstimateProblem(undefined);
    setDueDateProblem(undefined);
    try {
      const acceptanceCriteria = criteria
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      const created = await create.mutateAsync({
        title: title.trim(),
        description: description.trim(),
        priority,
        ...assigneePayload(assignee),
        acceptance_criteria: acceptanceCriteria,
        kind,
        parent_ticket_id: kind === 'task' && parentTicketId ? parentTicketId : null,
        ...(dueDate ? { due_date: dueDate } : {}),
        ...(estimateValue ? { estimated_effort: estimateValue } : {}),
      });
      addToast('success', `Created ${kind} "${title.trim()}"`);
      onClose();
      if (created?.id) onCreated?.(created);
    } catch (exc) {
      const assigneeProblem = assigneeErrorFrom(exc);
      if (assigneeProblem) setAssigneeError(assigneeProblem);
      // A 422 on the estimate or date names the field; show it there too.
      const fieldProblem = assigneeProblem ? null : scheduleErrorMessage(exc);
      const perField = scheduleFieldErrors(exc);
      if (perField.estimated_effort) setEstimateProblem(perField.estimated_effort);
      if (perField.due_date) setDueDateProblem(perField.due_date);
      if (perField.estimated_effort || perField.due_date) setMoreOpen(true);
      addToast('error', `Create failed: ${assigneeProblem ?? fieldProblem ?? errorMessage(exc)}`);
    } finally {
      setSubmitting(false);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLFormElement>) {
    if (e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return;
    e.preventDefault();
    formRef.current?.requestSubmit();
  }

  return (
    <Dialog open onClose={onClose} titleId="ticket-new-title" className="max-w-xl">
      <h2 id="ticket-new-title" className="mb-3 text-lg font-semibold text-text-primary">
        New ticket
      </h2>
      <form ref={formRef} onSubmit={handleSubmit} onKeyDown={handleKeyDown} className="tk-new-form space-y-3">
        <input
          ref={titleRef}
          id="field-title"
          type="text"
          className="tk-new-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          aria-label="Title"
          placeholder="Title"
          autoComplete="off"
        />

        <Textarea
          id="field-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          required
          rows={4}
          aria-label="Description"
          placeholder="Describe the work. Markdown works."
        />

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <span id="field-kind-label" className="text-sm font-medium text-text-secondary">
              Kind
            </span>
            <div className="tk-seg self-start" role="group" aria-labelledby="field-kind-label">
              <button type="button" aria-pressed={kind === 'task'} onClick={() => chooseKind('task')}>
                Task
              </button>
              <button type="button" aria-pressed={kind === 'issue'} onClick={() => chooseKind('issue')}>
                Issue
              </button>
            </div>
            <p className="text-xs text-text-tertiary">
              {kind === 'issue'
                ? 'Groups Tasks. Needs the project owner or an org admin.'
                : 'One unit of work for a person or an agent.'}
            </p>
          </div>

          {kind === 'task' && (
            <Select
              id="field-parent"
              value={parentTicketId}
              onValueChange={setParentTicketId}
              title="Part of Issue"
              options={[
                { value: '', label: 'None' },
                ...issueOptions.map((iss) => ({ value: iss.id, label: iss.title.length > 60 ? `${iss.title.slice(0, 59)}…` : iss.title })),
              ]}
            />
          )}
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 items-start">
          <Select
            id="field-priority"
            value={priority}
            onValueChange={(v) => setPriority(v as typeof priority)}
            title="Priority"
            options={[
              { value: 'low', label: 'Low' },
              { value: 'medium', label: 'Medium' },
              { value: 'high', label: 'High' },
              { value: 'critical', label: 'Critical' },
            ]}
          />

          <AssigneePicker
            id="field-assignee"
            projectId={projectId}
            orgId={orgId}
            value={assignee}
            onChange={(next) => {
              setAssignee(next);
              setAssigneeError(undefined);
            }}
            error={assigneeError}
          />
        </div>

        <details
          className="tk-more"
          open={moreOpen}
          onToggle={(e) => setMoreOpen((e.currentTarget as HTMLDetailsElement).open)}
        >
          <summary>More details: acceptance criteria, due date, estimate</summary>
          <div className="space-y-3 pt-3">
            <Textarea
              id="field-criteria"
              value={criteria}
              onChange={(e) => setCriteria(e.target.value)}
              rows={3}
              title="Acceptance criteria (one per line)"
              placeholder={'Migration adds the exports table\nThe route returns 404 for other projects\nTests cover both paths'}
            />
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 items-start">
              <Input
                id="field-due-date"
                type="date"
                value={dueDate}
                onChange={(e) => {
                  setDueDate(e.target.value);
                  setDueDateProblem(undefined);
                }}
                title="Due date"
                error={dueDateProblem}
              />
              <Input
                id="field-estimate"
                type="text"
                value={estimate}
                onChange={(e) => {
                  setEstimate(e.target.value);
                  setEstimateProblem(undefined);
                }}
                title="Estimate"
                placeholder="2d 4h"
                hint="Jira durations: w, d, h, m"
                error={estimateProblem}
              />
            </div>
          </div>
        </details>

        <DialogFooter>
          <span className="mr-auto hidden items-center gap-1 text-2xs text-text-tertiary sm:inline-flex" aria-hidden="true">
            <Kbd>{IS_MAC ? '⌘' : 'Ctrl'}</Kbd>
            <Kbd>↵</Kbd>
            to create
          </span>
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={submitting} aria-keyshortcuts={IS_MAC ? 'Meta+Enter' : 'Control+Enter'}>
            Create ticket
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
