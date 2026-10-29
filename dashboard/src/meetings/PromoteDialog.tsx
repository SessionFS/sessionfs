import { useEffect, useState, type FormEvent } from 'react';
import type { MeetingActionItem, MeetingPromotePreview } from '../api/client';
import { Alert, Button, Checkbox, Dialog, DialogFooter, DialogHeader, Input, Select, Textarea } from '../components/ui';
import { usePromoteMeetingItem, usePromotePreview } from '../hooks/useMeetings';
import { useToast } from '../hooks/useToast';
import { AssigneePicker } from '../projects/tickets/AssigneePicker';
import { UNASSIGNED, assigneePayload, type Assignee } from '../projects/tickets/assignee';
import { linesLabel, meetingActionError, meetingErrorCode } from './format';

interface PromoteDialogProps {
  meetingId: string;
  item: MeetingActionItem;
  onClose: () => void;
}

const PRIORITIES = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'critical', label: 'Critical' },
];

function draftAssignee(p: MeetingPromotePreview): Assignee {
  if (p.assigned_to) return { kind: 'persona', name: p.assigned_to };
  if (p.assigned_user_id) return { kind: 'person', userId: p.assigned_user_id };
  return UNASSIGNED;
}

/**
 * Create a ticket from one action item: the server's draft (title, the item
 * text with its quoted lines, the matched owner, the due date) shown editable,
 * then Confirm. A group meeting picks the project to file it in.
 */
export function PromoteDialog({ meetingId, item, onClose }: PromoteDialogProps) {
  const preview = usePromotePreview(meetingId, item.id);
  // While the ticket is being created the dialog stays: closing it would
  // look like a cancel of a write that still lands.
  const [creating, setCreating] = useState(false);
  const close = () => {
    if (!creating) onClose();
  };
  const range = { start: item.line_start, end: item.line_end };

  return (
    <Dialog open onClose={close} titleId="meeting-promote-title" className="max-w-2xl">
      <DialogHeader titleId="meeting-promote-title">Create ticket</DialogHeader>
      <p className="-mt-2 mb-4 text-sm text-text-tertiary">
        From an action item, {linesLabel(range).toLowerCase()}. Edit anything before you create it.
      </p>

      {preview.isLoading && (
        <div className="space-y-3" aria-busy="true" aria-label="Loading the draft">
          <div className="h-9 rounded-md bg-bg-elevated animate-pulse" />
          <div className="h-40 rounded-md bg-bg-elevated animate-pulse" />
          <div className="h-9 rounded-md bg-bg-elevated animate-pulse" />
        </div>
      )}

      {preview.isError && (
        <>
          <Alert variant="danger">{meetingActionError(preview.error)}</Alert>
          <DialogFooter>
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
          </DialogFooter>
        </>
      )}

      {preview.data && (
        <PromoteForm
          meetingId={meetingId}
          itemId={item.id}
          draft={preview.data}
          onClose={onClose}
          onPendingChange={setCreating}
        />
      )}
    </Dialog>
  );
}

function PromoteForm({
  meetingId,
  itemId,
  draft,
  onClose,
  onPendingChange,
}: {
  meetingId: string;
  itemId: string;
  draft: MeetingPromotePreview;
  onClose: () => void;
  onPendingChange: (pending: boolean) => void;
}) {
  const promote = usePromoteMeetingItem(meetingId);
  const pending = promote.isPending;
  useEffect(() => onPendingChange(pending), [pending, onPendingChange]);
  const { addToast } = useToast();

  const [title, setTitle] = useState(draft.title);
  const [description, setDescription] = useState(draft.description);
  const [priority, setPriority] = useState<MeetingPromotePreview['priority']>(draft.priority);
  const [assignee, setAssignee] = useState<Assignee>(() => draftAssignee(draft));
  const [dueDate, setDueDate] = useState(draft.due_date ?? '');
  const [target, setTarget] = useState(draft.target_project_id ?? '');
  const [publishJira, setPublishJira] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [assigneeError, setAssigneeError] = useState<string | undefined>();

  const isGroup = draft.target_project_id == null;
  const options = draft.target_project_options ?? [];
  // Whether "Also publish to Jira" applies comes from the server's draft
  // (`jira_publish_available`, the publish preflight's DB-only gates). A
  // group meeting asks again for the chosen target project.
  const targetCheck = usePromotePreview(meetingId, itemId, target || undefined, isGroup && !!target);
  const jiraAvailable = isGroup
    ? !!target && targetCheck.data?.jira_publish_available === true
    : draft.jira_publish_available === true;

  function changeTarget(next: string) {
    setTarget(next);
    // A person or persona is checked against the target project's assignee
    // set; keep the draft's owner and let the server say if it doesn't fit.
    setAssigneeError(undefined);
    setPublishJira(false);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (isGroup && !target) {
      setError('Choose the project to create the ticket in.');
      return;
    }
    setError(null);
    setAssigneeError(undefined);
    promote.mutate(
      {
        itemId,
        body: {
          title: title.trim(),
          description: description.trim(),
          priority,
          kind: 'task',
          ...assigneePayload(assignee),
          due_date: dueDate || null,
          ...(isGroup ? { target_project_id: target } : {}),
          ...(jiraAvailable && publishJira ? { publish_to_jira: true } : {}),
        },
      },
      {
        onSuccess: (result) => {
          const jiraFailed = result.warnings.some((w) => w.startsWith('jira_publish_failed'));
          addToast('success', `Created ticket ${result.ticket_id}`);
          if (jiraFailed) {
            addToast('warning', 'The ticket was created, but publishing it to Jira failed. Publish it from the ticket.');
          }
          onClose();
        },
        onError: (err) => {
          const code = meetingErrorCode(err);
          if (code === 'assignee_not_eligible' || code === 'assignee_conflict') {
            setAssigneeError(meetingActionError(err));
          } else {
            setError(meetingActionError(err));
          }
        },
      },
    );
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      {isGroup && (
        options.length > 0 ? (
          <Select
            id="promote-target"
            title="Project"
            value={target}
            onValueChange={changeTarget}
            placeholder="Choose a project"
            options={[...options].sort((a, b) => a.name.localeCompare(b.name)).map((p) => ({ value: p.id, label: p.name }))}
            hint="Group meetings can file tickets in any group project you can edit."
          />
        ) : (
          <Alert variant="warning">You can’t create tickets in any of this group’s projects.</Alert>
        )
      )}

      <Input
        id="promote-title"
        title="Title"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        maxLength={200}
        required
      />

      <Textarea
        id="promote-description"
        title="Description"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        rows={9}
        required
        className="font-mono text-xs leading-5"
        hint="Includes the quoted transcript lines, so the ticket keeps its context."
      />

      {target ? (
        <AssigneePicker
          id="promote-assignee"
          projectId={target}
          value={assignee}
          onChange={(next) => {
            setAssignee(next);
            setAssigneeError(undefined);
          }}
          error={assigneeError}
        />
      ) : (
        <Input id="promote-assignee-disabled" title="Assignee" value="" placeholder="Choose a project first" disabled readOnly />
      )}

      <div className="grid gap-3 sm:grid-cols-2 items-start">
        <Select
          id="promote-priority"
          title="Priority"
          value={priority}
          onValueChange={(v) => setPriority(v as MeetingPromotePreview['priority'])}
          options={PRIORITIES}
        />
        <Input
          id="promote-due"
          type="date"
          title="Due date (optional)"
          value={dueDate}
          onChange={(e) => setDueDate(e.target.value)}
        />
      </div>

      {jiraAvailable && (
        <Checkbox
          id="promote-jira"
          checked={publishJira}
          onChange={(e) => setPublishJira(e.target.checked)}
          label="Also publish to Jira"
          description="Publishes to the project’s mapped Jira project. If Jira fails after the ticket is created, the ticket stays and you can publish it later."
        />
      )}

      {error && <Alert variant="danger">{error}</Alert>}

      <DialogFooter>
        <Button variant="secondary" type="button" onClick={onClose} disabled={promote.isPending}>
          Cancel
        </Button>
        <Button
          variant="primary"
          type="submit"
          loading={promote.isPending}
          disabled={promote.isPending || (isGroup && options.length === 0)}
        >
          Create ticket
        </Button>
      </DialogFooter>
    </form>
  );
}

