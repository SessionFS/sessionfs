import { useState, type FormEvent, type ReactNode } from 'react';
import { useUpdateProjectSettings, useUpdateProjectName } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { useMyOrgs } from '../transfers/useTransfers';
import type { ProjectContext } from '../api/client';
import { Card, Button } from '../components/ui';
import ReposTab from './ReposTab';
import MergeSurface from './MergeSurface';
import TransferPanel from './TransferPanel';
import { errorMessage } from '../utils/errorMessage';

interface ProjectSettingsProps {
  project: ProjectContext;
  onRequestDelete: () => void;
}

function SettingsCard({ children }: { children: ReactNode }) {
  return (
    <Card level="elevated" className="rounded-xl shadow-[var(--shadow-sm)]">
      {children}
    </Card>
  );
}

/**
 * Everything that changes the project itself rather than its content:
 * rename, auto-narrative, linked repos, merge, ownership transfer and
 * delete. Merge and Transfer keep the headings they already render.
 */
export default function ProjectSettings({ project, onRequestDelete }: ProjectSettingsProps) {
  const { addToast } = useToast();
  const myOrgs = useMyOrgs();
  const updateSettings = useUpdateProjectSettings(project.id);
  const updateName = useUpdateProjectName(project.id);

  const [nameDraft, setNameDraft] = useState(project.name || '');
  const [autoNarrative, setAutoNarrative] = useState(project.auto_narrative ?? false);

  const trimmed = nameDraft.trim();
  const nameUnchanged = trimmed === (project.name || '');

  function handleRename(e: FormEvent) {
    e.preventDefault();
    if (!trimmed || nameUnchanged) return;
    updateName.mutate(trimmed, {
      onSuccess: () => addToast('success', `Project renamed to "${trimmed}"`),
      onError: (err) => addToast('error', `Rename failed: ${errorMessage(err)}`),
    });
  }

  return (
    <div className="space-y-4">
      <SettingsCard>
        <section aria-labelledby="settings-name-heading" className="p-5">
          <h2 id="settings-name-heading" className="text-base font-semibold text-text-primary mb-1">
            Project name
          </h2>
          <p className="text-sm text-text-tertiary mb-3">
            Shown in the dashboard. The repo path stays {project.git_remote_normalized}.
          </p>
          <form onSubmit={handleRename} className="flex items-center gap-2">
            <input
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              aria-label="Project name"
              placeholder={project.git_remote_normalized}
              className="min-w-0 flex-1 max-w-md bg-bg-sunken border border-border rounded-lg px-3 py-1.5 text-sm text-text-primary outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            />
            <Button
              type="submit"
              size="sm"
              disabled={!trimmed || nameUnchanged || updateName.isPending}
              loading={updateName.isPending}
            >
              Rename
            </Button>
          </form>
        </section>
      </SettingsCard>

      <SettingsCard>
        <section aria-labelledby="settings-narrative-heading" className="p-5">
          <h2 id="settings-narrative-heading" className="text-base font-semibold text-text-primary mb-2">
            Auto-narrative
          </h2>
          <label className="flex items-center gap-2 text-sm text-text-secondary">
            <input
              type="checkbox"
              checked={autoNarrative}
              onChange={(e) => {
                const val = e.target.checked;
                setAutoNarrative(val);
                updateSettings.mutate(
                  { auto_narrative: val },
                  {
                    onSuccess: () => addToast('success', `Auto-narrative ${val ? 'enabled' : 'disabled'}.`),
                    onError: (err) => { setAutoNarrative(!val); addToast('error', `Update failed: ${errorMessage(err)}`); },
                  },
                );
              }}
              className="accent-[var(--brand)]"
            />
            Auto-narrative on sync
          </label>
        </section>
      </SettingsCard>

      <SettingsCard>
        <section aria-labelledby="settings-repos-heading">
          <h2 id="settings-repos-heading" className="px-5 pt-5 text-base font-semibold text-text-primary">
            Repositories
          </h2>
          <ReposTab projectId={project.id} />
        </section>
      </SettingsCard>

      <SettingsCard>
        <MergeSurface projectId={project.id} projectName={project.name || project.git_remote_normalized} />
      </SettingsCard>

      <SettingsCard>
        <div className="p-5">
          <TransferPanel
            projectId={project.id}
            currentScope={project.org_id ?? 'personal'}
            availableOrgs={myOrgs.data ?? []}
          />
        </div>
      </SettingsCard>

      <Card className="rounded-xl !border-danger/40">
        <section aria-labelledby="settings-danger-heading" className="p-5 flex items-center justify-between gap-4 flex-wrap">
          <div>
            <h2 id="settings-danger-heading" className="text-base font-semibold text-danger mb-1">
              Danger zone
            </h2>
            <p className="text-sm text-text-tertiary">
              Permanently delete this project and its context. This cannot be undone.
            </p>
          </div>
          <Button variant="danger" onClick={onRequestDelete}>Delete project</Button>
        </section>
      </Card>
    </div>
  );
}
