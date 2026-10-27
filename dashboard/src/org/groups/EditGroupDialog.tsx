import { useState, type FormEvent } from 'react';
import { Alert, Button, Dialog, DialogFooter, DialogHeader, Input, Textarea } from '../../components/ui';
import type { ProjectGroupDetail } from '../../api/client';
import { useToast } from '../../hooks/useToast';
import { groupErrorMessage } from '../../utils/groups';
import { useUpdateGroup } from './useProjectGroups';

const TITLE_ID = 'edit-group-title';

interface EditGroupDialogProps {
  group: ProjectGroupDetail;
  open: boolean;
  onClose: () => void;
}

/** Group admins rename a group and edit its description. The slug stays (CLI/API references use it). */
export default function EditGroupDialog({ group, open, onClose }: EditGroupDialogProps) {
  const update = useUpdateGroup(group.org_id, group.id);
  const { addToast } = useToast();
  const [name, setName] = useState(group.name);
  const [description, setDescription] = useState(group.description ?? '');
  const [submitted, setSubmitted] = useState(false);

  const nameError = submitted && !name.trim() ? 'Enter a name.' : undefined;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitted(true);
    if (!name.trim()) return;
    update.mutate(
      { name: name.trim(), description: description.trim() || null },
      {
        onSuccess: () => {
          addToast('success', 'Group updated.');
          onClose();
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={onClose} titleId={TITLE_ID}>
      <DialogHeader titleId={TITLE_ID}>Edit group</DialogHeader>
      <form onSubmit={handleSubmit} noValidate className="space-y-4">
        <Input
          title="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={255}
          error={nameError}
          autoFocus
        />
        <Textarea
          title="Description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
        />
        {update.isError && <Alert variant="danger">{groupErrorMessage(update.error)}</Alert>}
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" loading={update.isPending}>
            Save
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
