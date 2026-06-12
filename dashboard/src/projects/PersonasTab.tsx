/**
 * v0.10.3 — Personas tab (v0.10.1 backend, v0.10.3 dashboard surface).
 *
 * Lists agent personas for the project; supports create / edit / delete.
 * Personas are scoped per project (Pro+ tier) and used to give ticketed
 * agent work a stable role + content profile.
 *
 * Phase 3 restyle: migrated onto ui/ primitives (Table, Dialog, Button,
 * Input, Textarea).
 */

import { useState } from 'react';
import { useToast } from '../hooks/useToast';
import {
  useCreatePersona,
  useDeletePersona,
  usePersonas,
  useUpdatePersona,
} from '../hooks/usePersonas';
import RelativeDate from '../components/RelativeDate';
import { ApiError, type Persona } from '../api/client';
import { Button, Dialog, DialogHeader, DialogFooter, Input, Table, Textarea } from '../components/ui';

interface PersonasTabProps {
  projectId: string;
}

interface EditState {
  mode: 'create' | 'edit';
  persona?: Persona;
}

export default function PersonasTab({ projectId }: PersonasTabProps) {
  const { data, isLoading, error } = usePersonas(projectId);
  const create = useCreatePersona(projectId);
  const update = useUpdatePersona(projectId);
  const remove = useDeletePersona(projectId);
  const { addToast } = useToast();

  const [edit, setEdit] = useState<EditState | null>(null);
  const [deleting, setDeleting] = useState<Persona | null>(null);

  if (isLoading) return <p>Loading personas…</p>;
  if (error) return <p role="alert">Failed to load personas: {String(error)}</p>;
  if (!data) return null;

  return (
    <section aria-labelledby="personas-heading" className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 id="personas-heading" className="text-lg font-semibold">
          Personas
          <span className="ml-2 text-sm text-text-tertiary">
            {data.length} {data.length === 1 ? 'persona' : 'personas'}
          </span>
        </h2>
        <Button
          variant="primary"
          size="sm"
          onClick={() => setEdit({ mode: 'create' })}
        >
          New persona
        </Button>
      </div>

      {data.length === 0 ? (
        <div className="border border-border rounded-lg p-6 text-center text-sm text-text-tertiary space-y-1">
          <p className="font-medium text-md text-text-secondary">No personas yet.</p>
          <p>
            Personas are reusable agent roles you can attach to tickets so every
            captured session is tagged with the agent's identity.
          </p>
        </div>
      ) : (
        <Table<Persona>
          data={data}
          rowKey={(p) => p.id}
          columns={[
            { key: 'name', header: 'Name', render: (p) => <span className="font-mono text-text-primary">{p.name}</span> },
            { key: 'role', header: 'Role', render: (p) => <>{p.role}</> },
            {
              key: 'specs',
              header: 'Specializations',
              render: (p) => (
                <span className="text-text-tertiary">
                  {p.specializations.length === 0
                    ? '—'
                    : p.specializations.slice(0, 3).join(', ') +
                      (p.specializations.length > 3 ? ` +${p.specializations.length - 3}` : '')}
                </span>
              ),
            },
            {
              key: 'updated',
              header: 'Updated',
              render: (p) => (
                <span className="text-text-tertiary">
                  <RelativeDate iso={p.updated_at} />
                </span>
              ),
            },
            {
              key: 'actions',
              header: '',
              width: 'w-0',
              render: (p) => (
                <div className="flex items-center gap-1 justify-end whitespace-nowrap">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={(e) => {
                      (e as React.MouseEvent).stopPropagation();
                      setEdit({ mode: 'edit', persona: p });
                    }}
                  >
                    Edit
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={(e) => {
                      (e as React.MouseEvent).stopPropagation();
                      setDeleting(p);
                    }}
                  >
                    Delete
                  </Button>
                </div>
              ),
            },
          ]}
        />
      )}

      {edit && (
        <PersonaEditModal
          state={edit}
          onClose={() => setEdit(null)}
          onSubmit={async (body) => {
            try {
              if (edit.mode === 'create') {
                await create.mutateAsync(body);
                addToast('success', `Created persona "${body.name}"`);
              } else if (edit.persona) {
                await update.mutateAsync({
                  name: edit.persona.name,
                  body: {
                    role: body.role,
                    content: body.content,
                    specializations: body.specializations,
                  },
                });
                addToast('success', `Updated "${edit.persona.name}"`);
              }
              setEdit(null);
            } catch (exc) {
              const msg =
                exc instanceof ApiError ? `${exc.status}: ${exc.message}` : String(exc);
              addToast('error', msg);
            }
          }}
        />
      )}

      {deleting && (
        <DeleteConfirmModal
          persona={deleting}
          onClose={() => setDeleting(null)}
          onConfirm={async (force) => {
            try {
              await remove.mutateAsync({ name: deleting.name, force });
              addToast('success', `Deleted "${deleting.name}"`);
              setDeleting(null);
            } catch (exc) {
              const msg =
                exc instanceof ApiError ? `${exc.status}: ${exc.message}` : String(exc);
              addToast('error', msg);
            }
          }}
        />
      )}
    </section>
  );
}

interface EditModalProps {
  state: EditState;
  onClose: () => void;
  onSubmit: (body: {
    name: string;
    role: string;
    content: string;
    specializations: string[];
  }) => void | Promise<void>;
}

function PersonaEditModal({ state, onClose, onSubmit }: EditModalProps) {
  const [name, setName] = useState(state.persona?.name ?? '');
  const [role, setRole] = useState(state.persona?.role ?? '');
  const [content, setContent] = useState(state.persona?.content ?? '');
  const [specsText, setSpecsText] = useState(
    (state.persona?.specializations ?? []).join(', '),
  );
  const [submitting, setSubmitting] = useState(false);

  const titleId = state.mode === 'create' ? 'persona-new-title' : 'persona-edit-title';
  const heading =
    state.mode === 'create' ? 'New persona' : `Edit "${state.persona?.name}"`;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    const specializations = specsText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      await onSubmit({ name: name.trim(), role: role.trim(), content, specializations });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open onClose={onClose} titleId={titleId} className="max-w-2xl">
      <DialogHeader titleId={titleId}>{heading}</DialogHeader>
      <form onSubmit={handleSubmit} className="space-y-4">
        <Input
          id="field-name"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={state.mode === 'edit'}
          required
          maxLength={50}
          pattern="[A-Za-z0-9_-]{1,50}"
          title="Name"
          className="font-mono"
          placeholder="atlas"
        />
        <p className="text-xs text-text-tertiary -mt-3">
          ASCII only — letters, digits, dash, underscore. Immutable after create.
        </p>

        <Input
          id="field-role"
          type="text"
          value={role}
          onChange={(e) => setRole(e.target.value)}
          required
          maxLength={100}
          title="Role"
          placeholder="Backend Architect"
        />

        <Input
          id="field-specializations"
          type="text"
          value={specsText}
          onChange={(e) => setSpecsText(e.target.value)}
          title="Specializations (comma-separated)"
          placeholder="auth, db, frontend"
        />

        <Textarea
          id="field-content"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={12}
          title="Content (markdown)"
          className="font-mono"
          placeholder="# You are…\n\nPersonality, expertise, rules…"
        />

        <DialogFooter>
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={submitting}>
            {state.mode === 'create' ? 'Create' : 'Save'}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

interface DeleteModalProps {
  persona: Persona;
  onClose: () => void;
  onConfirm: (force: boolean) => void | Promise<void>;
}

function DeleteConfirmModal({ persona, onClose, onConfirm }: DeleteModalProps) {
  const [force, setForce] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  return (
    <Dialog open onClose={onClose} titleId="persona-delete-title">
      <DialogHeader titleId="persona-delete-title">Delete "{persona.name}"?</DialogHeader>
      <p className="text-sm text-text-secondary mb-3">
        The server refuses delete when non-terminal tickets still reference
        this persona. Check <code className="font-mono">--force</code> to
        override and orphan those tickets' assignments.
      </p>
      <label className="flex items-center gap-2 text-sm mb-4 text-text-secondary cursor-pointer">
        <input
          type="checkbox"
          checked={force}
          onChange={(e) => setForce(e.target.checked)}
          className="w-4 h-4 rounded border-border accent-[var(--brand)]"
        />
        <span>Force — proceed even if tickets reference this persona</span>
      </label>
      <DialogFooter>
        <Button variant="secondary" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="danger"
          loading={submitting}
          onClick={async () => {
            setSubmitting(true);
            try {
              await onConfirm(force);
            } finally {
              setSubmitting(false);
            }
          }}
        >
          Delete
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
