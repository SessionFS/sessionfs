import { useState, useRef } from 'react';
import { useCreateProject } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { createProjectSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import FieldError from '../components/FieldError';

function nameFromRemote(remote: string): string {
  // "github.com/acme/repo" -> "acme/repo"
  try {
    const cleaned = remote.replace(/^https?:\/\//, '').replace(/\.git$/, '');
    const parts = cleaned.split('/');
    if (parts.length >= 3) {
      return parts.slice(1).join('/');
    }
    if (parts.length === 2) {
      return parts.join('/');
    }
    return cleaned;
  } catch {
    return remote;
  }
}

interface Props {
  onClose: () => void;
  onCreated: (remote: string) => void;
}

export default function CreateProjectModal({ onClose, onCreated }: Props) {
  const [remote, setRemote] = useState('');
  const [errors, setErrors] = useState<FieldErrors>({});
  const createProject = useCreateProject();
  const { addToast } = useToast();
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef);

  const name = remote.trim() ? nameFromRemote(remote.trim()) : '';

  function handleBlur() {
    if (!remote.trim()) return;
    const result = createProjectSchema.safeParse({ git_remote_normalized: remote });
    if (!result.success) {
      setErrors(fieldErrorsFromZod(result.error));
    } else {
      setErrors({});
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = remote.trim();
    const result = createProjectSchema.safeParse({ git_remote_normalized: trimmed });
    if (!result.success) {
      setErrors(fieldErrorsFromZod(result.error));
      return;
    }
    setErrors({});

    createProject.mutate(
      { name: nameFromRemote(trimmed), git_remote_normalized: trimmed },
      {
        onSuccess: () => {
          addToast('success', 'Project created');
          onCreated(trimmed);
        },
        onError: (err) => {
          addToast('error', `Failed to create project: ${String(err)}`);
        },
      },
    );
  }

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 z-50 bg-black/50" onClick={onClose} onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }} />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-project-title"
          onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}
          className="pointer-events-auto w-full max-w-md bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-lg)] p-6"
        >
          <h2 id="create-project-title" className="text-lg font-semibold text-[var(--text-primary)] mb-4">New Project</h2>
          <form onSubmit={handleSubmit}>
            <label className="block text-sm text-[var(--text-secondary)] mb-1">
              Git Remote URL
            </label>
            <input
              type="text"
              value={remote}
              onChange={(e) => { setRemote(e.target.value); if (errors.git_remote_normalized) setErrors({}); }}
              onBlur={handleBlur}
              placeholder="github.com/acme/repo"
              autoFocus
              className="w-full px-3 py-2 text-sm bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] placeholder:text-[var(--text-tertiary)]"
            />
            <FieldError message={errors.git_remote_normalized} />
            {name && (
              <p className="mt-2 text-xs text-[var(--text-tertiary)]">
                Project name: <span className="text-[var(--text-secondary)] font-medium">{name}</span>
              </p>
            )}
            <div className="flex justify-end gap-3 mt-6">
              <button
                type="button"
                onClick={onClose}
                className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!remote.trim() || createProject.isPending}
                className="px-4 py-1.5 text-sm bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {createProject.isPending ? 'Creating...' : 'Create'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}
