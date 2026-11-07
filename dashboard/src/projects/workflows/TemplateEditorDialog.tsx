import { useId, useState, type FormEvent } from 'react';
import { Alert, Button, Dialog, DialogFooter, DialogHeader, Textarea } from '../../components/ui';
import type { TemplateTarget, WorkflowTemplate } from '../../api/workflows';
import { useSaveWorkflowTemplate } from '../../hooks/useWorkflows';
import { errorMessage } from '../../utils/errorMessage';
import { NEW_TEMPLATE_TEXT, parseTemplateText, templateToYaml, TemplateTextError } from './templateText';

/**
 * Create or edit a template as YAML or JSON text. Editing saves a new
 * version (templates are immutable); loops keep the version they started with.
 * Server validation errors are shown next to the text, and the dialog stays
 * open and locked while the request runs.
 */
export function TemplateEditorDialog({
  target,
  template,
  onClose,
}: {
  target: TemplateTarget;
  /** The template to edit, or undefined to create one. */
  template?: WorkflowTemplate;
  onClose: () => void;
}) {
  const titleId = useId();
  const textId = useId();
  const errorId = useId();
  const save = useSaveWorkflowTemplate(target);
  const pending = save.isPending;
  const [text, setText] = useState(() =>
    template?.definition ? templateToYaml(template.name, template.definition) : NEW_TEMPLATE_TEXT,
  );
  const [localError, setLocalError] = useState<string | null>(null);
  const error = localError ?? (save.error ? errorMessage(save.error, 'Could not save the template') : null);
  const close = () => {
    if (!pending) onClose();
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pending) return;
    let body;
    try {
      body = parseTemplateText(text);
    } catch (err) {
      setLocalError(err instanceof TemplateTextError ? err.message : 'The template could not be read.');
      return;
    }
    setLocalError(null);
    try {
      await save.mutateAsync({ templateId: template?.id, body });
      onClose();
    } catch {
      /* shown inline; the dialog stays open so it can be fixed and retried */
    }
  }

  const heading = template ? `Edit ${template.name}` : 'New workflow template';
  return (
    <Dialog open onClose={close} titleId={titleId} className="max-w-3xl">
      <form onSubmit={submit} noValidate>
        <DialogHeader titleId={titleId}>{heading}</DialogHeader>
        <div className="space-y-3">
          <p className="text-sm text-text-secondary">
            Write the template as YAML or JSON, the same file <code className="font-mono text-text-primary">sfs workflow create</code> reads.
            {template && ` Saving creates version ${template.version + 1}; loops already running keep version ${template.version}.`}
          </p>
          {error && (
            <div id={errorId}>
              <Alert variant="danger">{error}</Alert>
            </div>
          )}
          <Textarea
            id={textId}
            title="Template"
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={pending}
            spellCheck={false}
            rows={16}
            aria-describedby={error ? errorId : undefined}
            aria-invalid={error ? true : undefined}
            className="font-mono text-xs"
          />
        </div>
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={close} disabled={pending}>Cancel</Button>
          <Button type="submit" loading={pending}>{template ? 'Save new version' : 'Create template'}</Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
