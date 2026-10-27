import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, Button, Card, Combobox, MonoText } from '../../components/ui';
import type { ProjectContext, ProjectGroupDetail } from '../../api/client';
import { useProjects } from '../../hooks/useProjects';
import { useToast } from '../../hooks/useToast';
import { UPGRADE_MESSAGE, groupErrorMessage } from '../../utils/groups';
import ConfirmDialog from './ConfirmDialog';
import { useAddGroupProject, useRemoveGroupProject } from './useProjectGroups';

interface GroupProjectsSectionProps {
  group: ProjectGroupDetail;
  /** Group admin standing: add and remove projects. */
  canManage: boolean;
  /** Org admins may add any org project; other group admins only ones they can edit. */
  isOrgAdmin: boolean;
  /** False when the org's plan lacks project groups: adding is blocked, removing still works. */
  planAllows: boolean;
}

function projectLabel(p: ProjectContext): string {
  return p.name || p.git_remote_normalized;
}

export default function GroupProjectsSection({ group, canManage, isOrgAdmin, planAllows }: GroupProjectsSectionProps) {
  const { addToast } = useToast();
  const projects = useProjects();
  const add = useAddGroupProject(group.org_id, group.id);
  const remove = useRemoveGroupProject(group.org_id, group.id);
  const [selected, setSelected] = useState('');
  const [removeTarget, setRemoveTarget] = useState<{ id: string; label: string } | null>(null);

  const byId = useMemo(() => new Map((projects.data ?? []).map((p) => [p.id, p])), [projects.data]);
  const inGroup = useMemo(() => new Set(group.projects), [group.projects]);

  // The server lets an org admin add any org project and any other group
  // admin only projects they can edit; offer exactly those.
  const candidates = useMemo(
    () =>
      (projects.data ?? [])
        .filter((p) => p.org_id === group.org_id && !inGroup.has(p.id))
        .filter((p) => isOrgAdmin || p.can_edit !== false)
        .map((p) => ({ value: p.id, label: projectLabel(p) }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [projects.data, group.org_id, inGroup, isOrgAdmin],
  );

  function handleAdd() {
    if (!selected) return;
    const label = candidates.find((c) => c.value === selected)?.label ?? selected;
    add.mutate(selected, {
      onSuccess: () => {
        setSelected('');
        addToast('success', `Added ${label} to ${group.name}.`);
      },
    });
  }

  function confirmRemove() {
    if (!removeTarget) return;
    const target = removeTarget;
    remove.mutate(target.id, {
      onSuccess: () => {
        setRemoveTarget(null);
        addToast('success', `Removed ${target.label} from ${group.name}.`);
      },
    });
  }

  const rows = group.projects.map((id) => {
    const p = byId.get(id);
    return { id, project: p, label: p ? projectLabel(p) : null };
  });

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="group-projects-heading" className="p-5">
        <h2 id="group-projects-heading" className="text-lg font-semibold text-text-primary">
          Projects
        </h2>
        <p className="mt-1 mb-4 text-sm text-text-tertiary">
          Everyone with access to this group can open these projects. Editors and admins can also change them.
        </p>

        {canManage && planAllows && (
          <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end">
            <div className="min-w-0 flex-1">
              <Combobox
                title="Add a project"
                value={selected}
                onValueChange={(v) => {
                  setSelected(v);
                  add.reset();
                }}
                options={candidates}
                placeholder={projects.isLoading ? 'Loading projects…' : 'Choose a project'}
                searchPlaceholder="Search projects…"
                loading={projects.isLoading}
                emptyMessage={
                  isOrgAdmin
                    ? 'Every project in this organization is already in the group.'
                    : 'No other organization projects you can edit.'
                }
              />
            </div>
            <Button onClick={handleAdd} disabled={!selected} loading={add.isPending} className="shrink-0">
              Add project
            </Button>
          </div>
        )}
        {canManage && !planAllows && (
          <Alert variant="info" className="mb-4">
            {UPGRADE_MESSAGE} You can still remove projects from this group.
          </Alert>
        )}
        {add.isError && (
          <Alert variant="danger" className="mb-4">
            {groupErrorMessage(add.error)}
          </Alert>
        )}

        {projects.isError && (
          <Alert variant="danger" className="mb-4">
            Couldn’t load project details: {groupErrorMessage(projects.error)}. The project ids are shown instead.
          </Alert>
        )}

        {rows.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-text-tertiary">
            {canManage ? 'No projects yet. Add one above.' : 'This group has no projects yet.'}
          </div>
        ) : (
          <ul className="divide-y divide-[var(--border)] rounded-lg border border-border" aria-label="Projects in this group">
            {rows.map(({ id, project, label }) => (
              <li key={id} className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  {project ? (
                    <>
                      <Link
                        to={`/projects/${encodeURIComponent(id)}`}
                        className="block truncate font-medium text-text-primary hover:text-brand hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-sm"
                      >
                        {label}
                      </Link>
                      <MonoText value={project.git_remote_normalized} className="text-xs text-text-tertiary" />
                    </>
                  ) : (
                    <>
                      <span className="block text-sm text-text-secondary">
                        {projects.isLoading
                          ? 'Loading…'
                          : projects.isError
                            ? 'Project details unavailable'
                            : 'A project you can’t open'}
                      </span>
                      <MonoText value={id} className="text-xs text-text-tertiary" />
                    </>
                  )}
                </div>
                {canManage && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      remove.reset();
                      setRemoveTarget({ id, label: label ?? id });
                    }}
                    aria-label={`Remove ${label ?? id} from the group`}
                  >
                    Remove
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <ConfirmDialog
        open={!!removeTarget}
        title="Remove project from group"
        confirmLabel="Remove project"
        danger
        pending={remove.isPending}
        error={remove.isError ? groupErrorMessage(remove.error) : null}
        onConfirm={confirmRemove}
        onClose={() => setRemoveTarget(null)}
      >
        <p>
          Remove <strong className="text-text-primary">{removeTarget?.label}</strong> from {group.name}?
        </p>
        <p>
          People who reach it only through this group lose access when the organization restricts project
          visibility. The project itself is not changed.
        </p>
      </ConfirmDialog>
    </Card>
  );
}
