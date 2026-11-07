import { useState } from 'react';
import { Alert, Badge, Button, Card } from '../../components/ui';
import type { StartedWorkflow, TemplateTarget, WorkflowTemplate } from '../../api/workflows';
import { useDisableWorkflowTemplate, useWorkflowTemplates } from '../../hooks/useWorkflows';
import { errorMessage } from '../../utils/errorMessage';
import { StartWorkflowDialog } from './StartWorkflowDialog';
import { TemplateEditorDialog } from './TemplateEditorDialog';
import { WorkflowGraph } from './WorkflowGraph';

const TEAM_TIERS = new Set(['team', 'enterprise', 'admin']);

/** Team and above, taken from the scope's own plan (a project's `effective_tier`, an org's `tier`). */
export function planAllowsWorkflows(tier: string | null | undefined): boolean {
  return !!tier && TEAM_TIERS.has(tier);
}

function TemplateRow({
  template,
  canEdit,
  canStart,
  onEdit,
  onStart,
  onDisable,
  disabling,
}: {
  template: WorkflowTemplate;
  canEdit: boolean;
  canStart: boolean;
  onEdit: () => void;
  onStart: () => void;
  onDisable: () => void;
  disabling: boolean;
}) {
  const [open, setOpen] = useState(false);
  const steps = template.definition?.steps ?? [];
  const panelId = `wft-graph-${template.id}`;
  return (
    <Card className="space-y-3 p-4" data-testid={`template-${template.id}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-text-primary">{template.name}</p>
          <p className="text-xs text-text-tertiary">
            Version {template.version} · {steps.length} {steps.length === 1 ? 'step' : 'steps'}
          </p>
        </div>
        <Badge variant={template.scope === 'org' ? 'info' : 'default'} label={template.scope === 'org' ? 'Org template' : 'Project template'} tint />
        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            size="sm"
            aria-label={`${open ? 'Hide' : 'Show'} graph for ${template.name}`}
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? 'Hide graph' : 'Show graph'}
          </Button>
          {canStart && (
            <Button variant="secondary" size="sm" aria-label={`Start ${template.name}`} onClick={onStart}>
              Start
            </Button>
          )}
          {canEdit && (
            <>
              <Button variant="secondary" size="sm" aria-label={`Edit ${template.name}`} onClick={onEdit}>
                Edit
              </Button>
              <Button variant="secondary" size="sm" aria-label={`Disable ${template.name}`} onClick={onDisable} loading={disabling} disabled={disabling}>
                Disable
              </Button>
            </>
          )}
        </div>
      </div>
      <div id={panelId} hidden={!open}>
        {open &&
          (template.definition ? (
            <WorkflowGraph steps={steps} label={`${template.name} graph`} />
          ) : (
            <Alert variant="warning">This version can no longer be read. Save a new version to repair it.</Alert>
          ))}
      </div>
    </Card>
  );
}

/**
 * Workflow templates for a project (Rules & Skills → Workflows) or an org
 * (Organization → Governance → Workflow templates). Read-only graph for everyone who can read;
 * create, edit and disable for editors; Start for loop writers on a project.
 */
export function TemplatesPanel({
  target,
  tier,
  canEdit,
  onStarted,
}: {
  target: TemplateTarget;
  /** The plan that pays for this scope. */
  tier: string | null | undefined;
  canEdit: boolean;
  /** Project scope only: called with the started loop. */
  onStarted?: (result: StartedWorkflow) => void;
}) {
  const allowed = planAllowsWorkflows(tier);
  const list = useWorkflowTemplates(target, allowed);
  const disable = useDisableWorkflowTemplate(target);
  const [editing, setEditing] = useState<WorkflowTemplate | 'new' | null>(null);
  const [starting, setStarting] = useState<string | null>(null);
  const [disablingId, setDisablingId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<WorkflowTemplate | null>(null);
  const projectScope = target.kind === 'project';

  if (!allowed) {
    return (
      <Card className="p-6" data-testid="workflows-gated">
        <p className="text-md font-semibold text-text-primary">Workflow templates are a Team feature</p>
        <p className="mt-1 max-w-xl text-sm text-text-tertiary">
          A template is a repeatable multi-agent plan: dispatch, fan-out implement, review, verify, document. SessionFS stores it, shows the plan and gates each step; your own orchestrator runs the agents.
          Upgrade to Team to write and start templates.
        </p>
      </Card>
    );
  }

  const templates = list.data ?? [];
  // Org templates are written from the org page; a project lists them read-only.
  const editable = (t: WorkflowTemplate) => canEdit && (projectScope ? t.scope === 'project' : true);
  return (
    <section aria-label="Workflow templates" className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-md font-semibold text-text-primary">Workflow templates</h2>
          <p className="text-sm text-text-tertiary">
            Repeatable multi-agent plans. SessionFS records and gates; your orchestrator starts every agent.
          </p>
        </div>
        {projectScope && canEdit && templates.length > 0 && (
          <Button variant="secondary" onClick={() => setStarting('')}>Start workflow</Button>
        )}
        {canEdit && <Button onClick={() => setEditing('new')}>New template</Button>}
      </div>

      {list.isLoading && <p className="text-sm text-text-tertiary">Loading templates…</p>}
      {list.error && <Alert variant="danger">Could not load templates: {errorMessage(list.error)}</Alert>}
      {disable.error && <Alert variant="danger">Could not disable the template: {errorMessage(disable.error)}</Alert>}
      {!list.isLoading && !list.error && templates.length === 0 && (
        <Card className="p-6 text-center" data-testid="templates-empty">
          <p className="text-md font-semibold text-text-primary">No templates yet</p>
          <p className="mx-auto mt-1 max-w-xl text-sm text-text-tertiary">
            Write one as YAML or JSON, or create it from the terminal with <code className="font-mono text-text-primary">sfs workflow create</code>.
          </p>
        </Card>
      )}
      <div className="space-y-3">
        {templates.map((t) => (
          <TemplateRow
            key={t.id}
            template={t}
            canEdit={editable(t)}
            canStart={projectScope && canEdit}
            onEdit={() => setEditing(t)}
            onStart={() => setStarting(t.id)}
            onDisable={() => setConfirm(t)}
            disabling={disablingId === t.id}
          />
        ))}
      </div>

      {confirm && (
        <Alert
          variant="warning"
          title={`Disable ${confirm.name}?`}
          action={
            <span className="flex gap-2">
              <Button variant="secondary" size="sm" onClick={() => setConfirm(null)}>Keep</Button>
              <Button
                variant="danger"
                size="sm"
                onClick={async () => {
                  const t = confirm;
                  setConfirm(null);
                  setDisablingId(t.id);
                  try {
                    await disable.mutateAsync(t.id);
                  } catch {
                    /* shown above */
                  } finally {
                    setDisablingId(null);
                  }
                }}
              >
                Disable template
              </Button>
            </span>
          }
        >
          It can no longer be started. Loops already running keep working.
        </Alert>
      )}

      {editing && (
        <TemplateEditorDialog target={target} template={editing === 'new' ? undefined : editing} onClose={() => setEditing(null)} />
      )}
      {starting !== null && projectScope && (
        <StartWorkflowDialog
          projectId={target.id}
          templates={templates}
          initialTemplateId={starting || undefined}
          onClose={() => setStarting(null)}
          onStarted={onStarted}
        />
      )}
    </section>
  );
}
