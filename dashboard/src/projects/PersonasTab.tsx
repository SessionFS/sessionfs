/**
 * v0.10.3 — Personas tab (v0.10.1 backend, v0.10.3 dashboard surface).
 *
 * Lists agent personas for the project; supports create / edit / delete.
 * Personas are scoped per project (Pro+ tier) and used to give ticketed
 * agent work a stable role + content profile.
 */

import { useEffect, useRef, useState } from 'react';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { useToast } from '../hooks/useToast';
import {
  useCreatePersona,
  useDeletePersona,
  usePersonas,
  useUpdatePersona,
} from '../hooks/usePersonas';
import RelativeDate from '../components/RelativeDate';
import { ApiError, type Persona } from '../api/client';

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
    <section aria-labelledby="personas-heading">
      <div className="flex items-center justify-between mb-4">
        <h2 id="personas-heading" className="text-lg font-semibold">
          Personas
          <span className="ml-2 text-sm text-muted">
            {data.length} {data.length === 1 ? 'persona' : 'personas'}
          </span>
        </h2>
        <button
          type="button"
          className="px-3 py-1.5 text-sm rounded bg-brand text-white hover:brightness-110"
          onClick={() => setEdit({ mode: 'create' })}
        >
          New persona
        </button>
      </div>

      {data.length === 0 ? (
        <div className="border border-border rounded p-6 text-center text-muted">
          No personas yet. Personas are reusable agent roles you can attach to
          tickets so every captured session is tagged with the agent's identity.
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-2 px-2">Name</th>
              <th className="py-2 px-2">Role</th>
              <th className="py-2 px-2">Specializations</th>
              <th className="py-2 px-2">Updated</th>
              <th className="py-2 px-2 w-0"></th>
            </tr>
          </thead>
          <tbody>
            {data.map((p) => (
              <tr key={p.id} className="border-t border-border">
                <td className="py-2 px-2 font-mono">{p.name}</td>
                <td className="py-2 px-2">{p.role}</td>
                <td className="py-2 px-2 text-muted">
                  {p.specializations.length === 0
                    ? '—'
                    : p.specializations.slice(0, 3).join(', ') +
                      (p.specializations.length > 3 ? ` +${p.specializations.length - 3}` : '')}
                </td>
                <td className="py-2 px-2 text-muted">
                  <RelativeDate iso={p.updated_at} />
                </td>
                <td className="py-2 px-2 whitespace-nowrap text-right">
                  <button
                    type="button"
                    className="px-2 py-1 text-xs rounded border border-border hover:bg-surface mr-1"
                    onClick={() => setEdit({ mode: 'edit', persona: p })}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="px-2 py-1 text-xs rounded border border-border hover:bg-danger hover:text-white"
                    onClick={() => setDeleting(p)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
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
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef);

  const [name, setName] = useState(state.persona?.name ?? '');
  const [role, setRole] = useState(state.persona?.role ?? '');
  const [content, setContent] = useState(state.persona?.content ?? '');
  const [specsText, setSpecsText] = useState(
    (state.persona?.specializations ?? []).join(', '),
  );
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

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
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="persona-edit-title"
        className="bg-bg border border-border rounded p-5 w-full max-w-2xl max-h-[85vh] overflow-y-auto"
      >
        <h3 id="persona-edit-title" className="text-base font-semibold mb-3">
          {state.mode === 'create' ? 'New persona' : `Edit "${state.persona?.name}"`}
        </h3>
        <form onSubmit={handleSubmit} className="space-y-3">
          <label className="block">
            <span className="text-sm text-muted">Name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={state.mode === 'edit'}
              required
              maxLength={50}
              pattern="[A-Za-z0-9_-]{1,50}"
              title="1-50 ASCII characters: letters, digits, dash, underscore"
              className="w-full mt-1 px-2 py-1 border border-border rounded font-mono text-sm bg-surface disabled:opacity-60"
            />
            <span className="text-xs text-muted">
              ASCII only — letters, digits, dash, underscore. Immutable after create.
            </span>
          </label>

          <label className="block">
            <span className="text-sm text-muted">Role (one line)</span>
            <input
              type="text"
              value={role}
              onChange={(e) => setRole(e.target.value)}
              required
              maxLength={100}
              className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface"
            />
          </label>

          <label className="block">
            <span className="text-sm text-muted">Specializations (comma-separated)</span>
            <input
              type="text"
              value={specsText}
              onChange={(e) => setSpecsText(e.target.value)}
              placeholder="auth, db, frontend"
              className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface"
            />
          </label>

          <label className="block">
            <span className="text-sm text-muted">Content (markdown)</span>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={12}
              className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface font-mono"
              placeholder="# You are…&#10;&#10;Personality, expertise, rules…"
            />
          </label>

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              className="px-3 py-1.5 text-sm rounded border border-border hover:bg-surface"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-3 py-1.5 text-sm rounded bg-brand text-white hover:brightness-110 disabled:opacity-50"
            >
              {submitting ? 'Saving…' : state.mode === 'create' ? 'Create' : 'Save'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface DeleteModalProps {
  persona: Persona;
  onClose: () => void;
  onConfirm: (force: boolean) => void | Promise<void>;
}

function DeleteConfirmModal({ persona, onClose, onConfirm }: DeleteModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef);
  const [force, setForce] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="persona-delete-title"
        className="bg-bg border border-border rounded p-5 w-full max-w-md"
      >
        <h3 id="persona-delete-title" className="text-base font-semibold mb-3">
          Delete "{persona.name}"?
        </h3>
        <p className="text-sm text-muted mb-3">
          The server refuses delete when non-terminal tickets still reference
          this persona. Check <code className="font-mono">--force</code> to
          override and orphan those tickets' assignments.
        </p>
        <label className="flex items-center gap-2 text-sm mb-4">
          <input
            type="checkbox"
            checked={force}
            onChange={(e) => setForce(e.target.checked)}
          />
          <span>Force — proceed even if tickets reference this persona</span>
        </label>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded border border-border hover:bg-surface"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={submitting}
            className="px-3 py-1.5 text-sm rounded bg-danger text-white hover:brightness-110 disabled:opacity-50"
            onClick={async () => {
              setSubmitting(true);
              try {
                await onConfirm(force);
              } finally {
                setSubmitting(false);
              }
            }}
          >
            {submitting ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
  );
}
