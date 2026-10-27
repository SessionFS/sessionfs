import { useState, type FormEvent, type ReactNode } from 'react';
import { useUpdateProjectSettings, useUpdateProjectName } from '../hooks/useProjects';
import { useToast } from '../hooks/useToast';
import { useMyOrgs } from '../transfers/useTransfers';
import type { ProjectContext } from '../api/client';
import { Alert, Card, Button, Checkbox, Select, Input } from '../components/ui';
import ReposTab from './ReposTab';
import MergeSurface from './MergeSurface';
import TransferPanel from './TransferPanel';
import ProjectGroupsBlock from './ProjectGroupsBlock';
import { errorMessage } from '../utils/errorMessage';

interface ProjectSettingsProps {
  project: ProjectContext;
  onRequestDelete: () => void;
}

/** A titled group of settings: one card, rows separated by hairlines. */
function SettingsGroup({ id, title, description, children }: { id: string; title: string; description?: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id}>
      <h2 id={id} className="text-base font-semibold text-text-primary">
        {title}
      </h2>
      {description && <p className="mt-0.5 text-sm text-text-tertiary">{description}</p>}
      <Card level="elevated" className="mt-3 rounded-xl shadow-[var(--shadow-sm)] divide-y divide-border">
        {children}
      </Card>
    </section>
  );
}

/** One setting: what it is on the left, its control on the right (stacked on phones). */
function SettingRow({ id, title, description, children }: { id: string; title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <div role="group" aria-labelledby={id} className="grid grid-cols-1 gap-x-8 gap-y-3 p-5 lg:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
      <div className="min-w-0">
        <h3 id={id} className="text-sm font-semibold text-text-primary">
          {title}
        </h3>
        {description && <p className="mt-1 text-sm text-text-tertiary">{description}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
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
  const [schedule, setSchedule] = useState<string>(project.compile_schedule ?? 'nightly');
  const [afterClaims, setAfterClaims] = useState(
    project.compile_after_claims != null ? String(project.compile_after_claims) : '',
  );

  const savedThreshold = project.compile_after_claims != null ? String(project.compile_after_claims) : '';
  const thresholdUnchanged = afterClaims.trim() === savedThreshold;

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

  // The settings route always requires auto_narrative, so fold the current
  // (possibly optimistic) value into every schedule/threshold write.
  function handleSchedule(next: string) {
    const previous = schedule;
    setSchedule(next);
    updateSettings.mutate(
      { auto_narrative: autoNarrative, compile_schedule: next as 'off' | 'nightly' | 'weekly' },
      {
        onSuccess: () => addToast('success', `Compile schedule set to ${next}.`),
        onError: (err) => {
          setSchedule(previous);
          addToast('error', `Update failed: ${errorMessage(err)}`);
        },
      },
    );
  }

  function handleApplyThreshold(e: FormEvent) {
    e.preventDefault();
    const trimmedInput = afterClaims.trim();
    // An empty input clears the threshold (the backend treats an explicit
    // null as "clear"), so the schedule becomes the only trigger again.
    if (!trimmedInput) {
      updateSettings.mutate(
        { auto_narrative: autoNarrative, compile_after_claims: null },
        {
          onSuccess: () => addToast('success', 'Pending-claim threshold cleared.'),
          onError: (err) => addToast('error', `Update failed: ${errorMessage(err)}`),
        },
      );
      return;
    }
    const parsed = Number(trimmedInput);
    if (!Number.isInteger(parsed) || parsed < 1) return;
    updateSettings.mutate(
      { auto_narrative: autoNarrative, compile_after_claims: parsed },
      {
        onSuccess: () => addToast('success', `Compile threshold set to ${parsed} pending claims.`),
        onError: (err) => addToast('error', `Update failed: ${errorMessage(err)}`),
      },
    );
  }

  // Project-admin standing (owner or org admin/owner). Older servers omit it:
  // fall back to showing the controls and let the server decide.
  const canAdmin = project.can_admin !== false;
  const adminNote = project.org_id
    ? 'Renaming, linking repositories and merging need the project owner or an admin of its organization; transferring needs an organization admin. A project group role never grants them.'
    : 'Renaming, linking repositories, merging and transferring need the project owner.';

  // Same rule as the server's delete_project: the owner or a platform admin
  // (org admins can't delete). Older servers omit can_delete: show it and let
  // the server decide.
  const canDelete = project.can_delete !== false;
  const canTransfer = project.can_transfer ?? canAdmin;
  const dangerZone = (
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
  );

  // Readers can view the project but not change it; every control on this
  // page is a change, so show why instead of controls the server would refuse.
  if (project.can_edit === false) {
    return (
      <div className="space-y-4">
        <Alert variant="info" title="You can view this project but not change it">
          {project.org_id
            ? "Its settings can be changed by the project owner, the organization's admins and owners, and its editors: every org member by default, or, when the organization restricts project visibility, only members a project group makes an editor or admin."
            : 'Only the project owner can change its settings.'}{' '}
          {adminNote} You can still read it and add knowledge entries, tickets and comments.
        </Alert>
        {project.groups && project.groups.length > 0 && (
          <SettingsGroup id="settings-access-heading" title="Access & ownership">
            <div className="p-5">
              <ProjectGroupsBlock groups={project.groups} />
            </div>
          </SettingsGroup>
        )}
        {project.can_delete === true && dangerZone}
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {!canAdmin && (
        <Alert variant="info" title="Some settings need project admin rights">
          {adminNote}
        </Alert>
      )}

      <SettingsGroup id="settings-general-heading" title="General">
        <SettingRow
          id="settings-name-heading"
          title="Project name"
          description={<>Shown in the dashboard. The repo path stays <span className="font-mono text-xs">{project.git_remote_normalized}</span>.</>}
        >
          {canAdmin ? (
            <form onSubmit={handleRename} className="flex flex-wrap items-center gap-2">
              <input
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                aria-label="Project name"
                placeholder={project.git_remote_normalized}
                className="min-w-0 flex-[1_1_14rem] max-w-md bg-bg-sunken border border-border rounded-lg px-3 py-2 text-sm text-text-primary outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
              />
              <Button
                type="submit"
                variant={nameUnchanged || !trimmed ? 'secondary' : 'primary'}
                disabled={!trimmed || nameUnchanged || updateName.isPending}
                loading={updateName.isPending}
              >
                Rename
              </Button>
            </form>
          ) : (
            <p className="text-sm font-medium text-text-primary">{project.name || project.git_remote_normalized}</p>
          )}
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup id="settings-knowledge-heading" title="Knowledge" description="How sessions become this project's context document.">
        <SettingRow
          id="settings-narrative-heading"
          title="Auto-narrative"
          description="Summarise each session in prose when it syncs, alongside the deterministic summary."
        >
          <Checkbox
            label="Auto-narrative on sync"
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
          />
        </SettingRow>
        <SettingRow
          id="settings-compile-heading"
          title="Scheduled KB compile"
          description="How often the scheduled sweep rebuilds the context document. Projects grandfathered before v0.17.2 are never auto-compiled by the sweep."
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-44">
              <Select
                value={schedule}
                onValueChange={handleSchedule}
                options={[
                  { value: 'off', label: 'Off' },
                  { value: 'nightly', label: 'Nightly' },
                  { value: 'weekly', label: 'Weekly' },
                ]}
                title="Schedule"
                aria-label="Compile schedule"
              />
            </div>
            <form onSubmit={handleApplyThreshold} className="flex min-w-0 flex-wrap items-end gap-2">
              <div className="w-44">
                <Input
                  type="number"
                  min={1}
                  value={afterClaims}
                  onChange={(e) => setAfterClaims(e.target.value)}
                  title="Compile after N claims"
                  placeholder="No threshold"
                />
              </div>
              <Button
                type="submit"
                variant={thresholdUnchanged ? 'secondary' : 'primary'}
                disabled={thresholdUnchanged || updateSettings.isPending}
              >
                Apply
              </Button>
            </form>
          </div>
          <p className="mt-2 text-xs text-text-tertiary">
            With a threshold, the project also compiles as soon as that many claims are pending,
            whatever the schedule. Leave it empty to compile on the schedule only.
          </p>
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup id="settings-repos-heading" title="Repositories" description="The git repositories whose sessions flow into this project.">
        <ReposTab projectId={project.id} canAdmin={canAdmin} />
        {canAdmin && (
          <MergeSurface projectId={project.id} projectName={project.name || project.git_remote_normalized} />
        )}
      </SettingsGroup>

      {((project.groups && project.groups.length > 0) || canTransfer) && (
        <SettingsGroup id="settings-access-heading" title="Access & ownership">
          {project.groups && project.groups.length > 0 && (
            <div className="p-5">
              <ProjectGroupsBlock groups={project.groups} />
            </div>
          )}
          {canTransfer && (
            <div className="p-5">
              <TransferPanel
                projectId={project.id}
                currentScope={project.org_id ?? 'personal'}
                availableOrgs={myOrgs.data ?? []}
              />
            </div>
          )}
        </SettingsGroup>
      )}

      {canDelete && dangerZone}
    </div>
  );
}
