import { useId, useState, type FormEvent } from 'react';
import { Alert, Button, Dialog, DialogFooter, DialogHeader, Input, Select, Textarea } from '../../components/ui';
import type { StartWorkflowBody, StartedWorkflow, WorkflowTemplate } from '../../api/workflows';
import { useStartWorkflow } from '../../hooks/useWorkflows';
import { errorMessage } from '../../utils/errorMessage';

function whole(raw: string, label: string): { value?: number; error?: string } {
  const t = raw.trim();
  if (t === '') return {};
  if (!/^\d+$/.test(t) || Number(t) < 1) return { error: `${label} must be a whole number of 1 or more.` };
  return { value: Number(t) };
}

/**
 * Start a workflow from an existing issue ticket or a fresh title, with
 * optional budgets. Starting creates the loop and its step tickets; it does
 * not start any agent. The form is locked while the request runs.
 */
export function StartWorkflowDialog({
  projectId,
  templates,
  initialTemplateId,
  onClose,
  onStarted,
}: {
  projectId: string;
  /** Active templates this project may use. */
  templates: WorkflowTemplate[];
  initialTemplateId?: string;
  onClose: () => void;
  onStarted?: (result: StartedWorkflow) => void;
}) {
  const titleId = useId();
  const start = useStartWorkflow(projectId);
  const pending = start.isPending;
  const [templateId, setTemplateId] = useState(
    initialTemplateId && templates.some((t) => t.id === initialTemplateId) ? initialTemplateId : (templates[0]?.id ?? ''),
  );
  const [source, setSource] = useState<'title' | 'ticket'>('title');
  const [ticketId, setTicketId] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [reviewer, setReviewer] = useState('');
  const [tokens, setTokens] = useState('');
  const [rounds, setRounds] = useState('');
  const [cost, setCost] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const close = () => {
    if (!pending) onClose();
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pending) return;
    const body: StartWorkflowBody = { template_id: templateId };
    let err: string | null = null;
    if (!templateId) err = 'Choose a template.';
    if (source === 'ticket') {
      if (!ticketId.trim()) err = err ?? 'Enter the id of the issue to adopt.';
      else body.ticket_id = ticketId.trim();
    } else {
      if (!title.trim()) err = err ?? 'Give the workflow a title.';
      else body.title = title.trim();
      if (description.trim()) body.description = description.trim();
    }
    if (reviewer.trim()) body.reviewer_persona = reviewer.trim();
    const t = whole(tokens, 'Token limit');
    const r = whole(rounds, 'Review round limit');
    err = err ?? t.error ?? r.error ?? null;
    if (t.value !== undefined) body.token_budget = t.value;
    if (r.value !== undefined) body.max_review_rounds = r.value;
    if (cost.trim() !== '') {
      const n = Number(cost.trim());
      if (!Number.isFinite(n) || n <= 0) err = err ?? 'Cost limit must be more than 0.';
      else body.cost_budget_usd = n;
    }
    setFieldError(err);
    if (err) return;
    try {
      const result = await start.mutateAsync(body);
      onStarted?.(result);
      onClose();
    } catch {
      /* shown below; the dialog stays open so it can be retried */
    }
  }

  return (
    <Dialog open onClose={close} titleId={titleId} className="max-w-xl">
      <form onSubmit={submit} noValidate>
        <DialogHeader titleId={titleId}>Start workflow</DialogHeader>
        <div className="space-y-4">
          <p className="text-sm text-text-secondary">
            This creates the loop and one ticket per step. Your own orchestrator starts the agents; SessionFS records and gates.
          </p>
          <Select
            title="Template"
            value={templateId}
            onValueChange={setTemplateId}
            disabled={pending}
            options={templates.map((t) => ({ value: t.id, label: `${t.name} (v${t.version}${t.scope === 'org' ? ', org' : ''})` }))}
            placeholder="No templates"
          />
          <Select
            title="Start from"
            value={source}
            onValueChange={(v) => setSource(v === 'ticket' ? 'ticket' : 'title')}
            disabled={pending}
            options={[
              { value: 'title', label: 'A new title' },
              { value: 'ticket', label: 'An existing issue' },
            ]}
          />
          {source === 'ticket' ? (
            <Input
              title="Issue ticket id"
              placeholder="tk_…"
              value={ticketId}
              onChange={(e) => setTicketId(e.target.value)}
              disabled={pending}
              hint="The issue is adopted as the loop's parent."
            />
          ) : (
            <>
              <Input title="Title" value={title} onChange={(e) => setTitle(e.target.value)} disabled={pending} maxLength={200} />
              <Textarea title="Description (optional)" value={description} onChange={(e) => setDescription(e.target.value)} disabled={pending} rows={3} />
            </>
          )}
          <Input
            title="Reviewer persona (optional)"
            placeholder="codex-reviewer"
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            disabled={pending}
          />
          <fieldset className="space-y-3" disabled={pending}>
            <legend className="text-sm font-medium text-text-primary">Limits (optional)</legend>
            <Input title="Token limit" inputMode="numeric" placeholder="No limit" value={tokens} onChange={(e) => setTokens(e.target.value)} />
            <Input title="Review round limit" inputMode="numeric" placeholder="No limit" value={rounds} onChange={(e) => setRounds(e.target.value)} />
            <Input
              title="Cost limit (USD)"
              inputMode="decimal"
              placeholder="No limit"
              value={cost}
              onChange={(e) => setCost(e.target.value)}
              hint="Dollar limits are for org owners and admins."
            />
          </fieldset>
        </div>
        {fieldError && <Alert variant="danger" className="mt-4">{fieldError}</Alert>}
        {start.error && <Alert variant="danger" className="mt-4">{errorMessage(start.error, 'Could not start the workflow')}</Alert>}
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={close} disabled={pending}>Cancel</Button>
          <Button
            type="submit"
            loading={pending}
            disabled={templates.length === 0}
            disabledReason="No workflow templates yet. Create one under Rules & Skills, Workflows."
          >
            Start workflow
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
