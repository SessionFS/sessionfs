import { useState, type FormEvent } from 'react';
import { Alert, Button, Dialog, DialogFooter, DialogHeader, Input, Textarea } from '../../components/ui';
import type { ProjectGroupDetail } from '../../api/client';
import { groupErrorMessage, slugError, slugifyGroupName } from '../../utils/groups';
import { useCreateGroup } from './useProjectGroups';

interface CreateGroupDialogProps {
  orgId: string;
  open: boolean;
  onClose: () => void;
  onCreated: (group: ProjectGroupDetail) => void;
}

const TITLE_ID = 'create-group-title';

/** Org admins create a group: name, a slug derived from it until edited, and a description. */
export default function CreateGroupDialog({ orgId, open, onClose, onCreated }: CreateGroupDialogProps) {
  const create = useCreateGroup(orgId);
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const effectiveSlug = slugTouched ? slug : slugifyGroupName(name);
  const nameError = submitted && !name.trim() ? 'Enter a name.' : undefined;
  // A name with no letters or digits derives an empty slug; the server would
  // invent one, but asking is clearer than a random suffix.
  const slugProblem = submitted || slugTouched ? slugError(effectiveSlug) : undefined;

  function reset() {
    setName('');
    setSlug('');
    setSlugTouched(false);
    setDescription('');
    setSubmitted(false);
    create.reset();
  }

  function close() {
    reset();
    onClose();
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitted(true);
    if (!name.trim() || slugError(effectiveSlug)) return;
    create.mutate(
      {
        name: name.trim(),
        slug: effectiveSlug,
        description: description.trim() || null,
      },
      {
        onSuccess: (group) => {
          reset();
          onCreated(group);
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={close} titleId={TITLE_ID}>
      <DialogHeader titleId={TITLE_ID}>New project group</DialogHeader>
      <form onSubmit={handleSubmit} noValidate className="space-y-4">
        <Input
          title="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={255}
          placeholder="Payments team"
          error={nameError}
          autoFocus
        />
        <Input
          title="Slug"
          value={effectiveSlug}
          onChange={(e) => {
            setSlugTouched(true);
            setSlug(e.target.value.toLowerCase());
          }}
          maxLength={100}
          placeholder="payments-team"
          error={slugProblem}
          hint={slugProblem ? undefined : 'Used in the CLI and API. Lowercase letters, numbers and hyphens.'}
          className="font-mono"
        />
        <Textarea
          title="Description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          placeholder="What these projects have in common, and who should have access."
        />
        {create.isError && (
          <Alert variant="danger">{groupErrorMessage(create.error)}</Alert>
        )}
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" loading={create.isPending}>
            Create group
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
