import { useState } from 'react';
import { Button } from '../../components/ui';
import type { StartedWorkflow } from '../../api/workflows';
import { useWorkflowTemplates } from '../../hooks/useWorkflows';
import { StartWorkflowDialog } from './StartWorkflowDialog';

/** "Start workflow" for the Review Loop page. Hidden until a template exists. */
export function StartWorkflowButton({
  projectId,
  onStarted,
}: {
  projectId: string;
  onStarted?: (result: StartedWorkflow) => void;
}) {
  const list = useWorkflowTemplates({ kind: 'project', id: projectId });
  const [open, setOpen] = useState(false);
  const templates = list.data ?? [];
  if (templates.length === 0) return null;
  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>Start workflow</Button>
      {open && (
        <StartWorkflowDialog
          projectId={projectId}
          templates={templates}
          onClose={() => setOpen(false)}
          onStarted={onStarted}
        />
      )}
    </>
  );
}
