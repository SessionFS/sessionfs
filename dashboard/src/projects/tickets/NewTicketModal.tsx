import { useEffect, useState } from 'react';
import { useToast } from '../../hooks/useToast';
import { useCreateTicket, useTickets } from '../../hooks/useTickets';
import { errorMessage } from '../../utils/errorMessage';
import {
  Button,
  Dialog,
  DialogHeader,
  DialogFooter,
  Input,
  Select,
  Textarea,
} from '../../components/ui';

/* ── New ticket modal ── */

interface NewModalProps {
  projectId: string;
  onClose: () => void;
}

export function NewTicketModal({ projectId, onClose }: NewModalProps) {
  const create = useCreateTicket(projectId);
  const { addToast } = useToast();

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [criteria, setCriteria] = useState('');
  const [priority, setPriority] = useState<
    'low' | 'medium' | 'high' | 'critical'
  >('medium');
  const [assignedTo, setAssignedTo] = useState('');
  const [kind, setKind] = useState<'task' | 'issue'>('task');
  const [parentTicketId, setParentTicketId] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);

  // Open + in_progress Issues are valid parents.
  const { data: openIssues } = useTickets(projectId, {
    kind: 'issue',
    status: 'open',
  });
  const { data: activeIssues } = useTickets(projectId, {
    kind: 'issue',
    status: 'in_progress',
  });
  const issueOptions = [...(openIssues ?? []), ...(activeIssues ?? [])];

  useEffect(() => {
    if (kind === 'issue' && parentTicketId) setParentTicketId('');
  }, [kind, parentTicketId]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const acceptanceCriteria = criteria
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      await create.mutateAsync({
        title: title.trim(),
        description: description.trim(),
        priority,
        assigned_to: assignedTo.trim() || null,
        acceptance_criteria: acceptanceCriteria,
        kind,
        parent_ticket_id:
          kind === 'task' && parentTicketId ? parentTicketId : null,
      });
      addToast('success', `Created ${kind} "${title.trim()}"`);
      onClose();
    } catch (exc) {
      addToast('error', `Create failed: ${errorMessage(exc)}`);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      titleId="ticket-new-title"
      className="max-w-2xl"
    >
      <DialogHeader titleId="ticket-new-title">New ticket</DialogHeader>
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Kind + Parent */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Select
              id="field-kind"
              value={kind}
              onValueChange={(v) =>
                setKind(v as 'task' | 'issue')
              }
              title="Kind"
              options={[
                { value: 'task', label: 'Task' },
                { value: 'issue', label: 'Issue' },
              ]}
            />
            <p className="text-xs text-text-tertiary mt-1">
              {kind === 'issue'
                ? 'PM-triaged container. Requires project owner or org admin.'
                : 'A single executor unit of work.'}
            </p>
          </div>

          {kind === 'task' && (
            <Select
              value={parentTicketId}
              onValueChange={setParentTicketId}
              title="Parent Issue (optional)"
              options={[
                { value: '', label: '— none —' },
                ...issueOptions.map((iss) => ({
                  value: iss.id,
                  label: `${iss.id} · ${iss.title.slice(0, 50)}`,
                })),
              ]}
            />
          )}
        </div>

        <Input
          id="field-title"
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          title="Title"
          placeholder="Implement session export"
        />

        <Textarea
          id="field-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          required
          rows={8}
          title="Description (markdown)"
          className="font-mono"
        />

        <Textarea
          id="field-criteria"
          value={criteria}
          onChange={(e) => setCriteria(e.target.value)}
          rows={4}
          title="Acceptance criteria (one per line)"
          placeholder="Migration adds ... table\nTier gate enforces ...\nTests cover ..."
        />

        <div className="grid grid-cols-2 gap-3">
          <Select
            id="field-priority"
            value={priority}
            onValueChange={(v) =>
              setPriority(v as typeof priority)
            }
            title="Priority"
            options={[
              { value: 'low', label: 'low' },
              { value: 'medium', label: 'medium' },
              { value: 'high', label: 'high' },
              { value: 'critical', label: 'critical' },
            ]}
          />

          <Input
            id="field-assign-to"
            type="text"
            value={assignedTo}
            onChange={(e) => setAssignedTo(e.target.value)}
            title="Assign to persona"
            placeholder="atlas, sentinel, ..."
            className="font-mono"
          />
        </div>

        <DialogFooter>
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={submitting}>
            Create
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
