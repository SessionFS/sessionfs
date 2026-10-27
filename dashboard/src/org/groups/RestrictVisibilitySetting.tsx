import { useState } from 'react';
import { Alert, Switch } from '../../components/ui';
import { useToast } from '../../hooks/useToast';
import { UPGRADE_MESSAGE, groupErrorMessage, isUpgradeRequired, orgHasProjectGroups } from '../../utils/groups';
import { useUpdateOrgSettings } from '../useOrgSettings';
import ConfirmDialog from './ConfirmDialog';

interface RestrictVisibilitySettingProps {
  orgId: string;
  /** Current value from GET /orgs/{org}/settings. */
  restricted: boolean;
  /** Org owner/admin. */
  canEdit: boolean;
  /** The org's plan tier, when known. */
  tier?: string | null;
}

/**
 * Org settings → "Restrict project visibility" (GitHub #101). Off keeps every
 * org project open to every member; on narrows each project to its owner, org
 * admins and the people a project group grants. Turning it on needs the
 * project_groups plan feature; turning it off is always allowed, so an org
 * that downgraded can still lift the restriction.
 */
export default function RestrictVisibilitySetting({ orgId, restricted, canEdit, tier }: RestrictVisibilitySettingProps) {
  const update = useUpdateOrgSettings(orgId);
  const { addToast } = useToast();
  const [pending, setPending] = useState<boolean | null>(null);
  const planAllows = orgHasProjectGroups(tier);
  // An error shows inside the confirmation while it is open, and inline only
  // once it is closed — never in both places at once.
  const dialogOpen = pending !== null;
  const inlineError = update.isError && !dialogOpen;
  const upgradeBlocked = !restricted && (!planAllows || (inlineError && isUpgradeRequired(update.error)));

  function apply(next: boolean) {
    update.mutate(
      { restrict_project_visibility: next },
      {
        onSuccess: () => {
          setPending(null);
          addToast(
            'success',
            next ? 'Project visibility is now restricted.' : 'Every member can see org projects again.',
          );
        },
      },
    );
  }

  const switchDisabled = !canEdit || update.isPending || (!restricted && !planAllows);

  return (
    <section aria-labelledby="org-visibility-heading" className="mt-8">
      <h3 id="org-visibility-heading">Project visibility</h3>
      <div className="mt-2 flex flex-col gap-3 rounded-lg border border-border bg-bg-primary p-4">
        <Switch
          checked={restricted}
          onChange={(next) => {
            update.reset();
            setPending(next);
          }}
          disabled={switchDisabled}
          label="Restrict project visibility"
          aria-describedby="org-visibility-help"
        />
        <div id="org-visibility-help" className="space-y-1.5 text-sm text-text-secondary">
          <p>
            <strong className="font-medium text-text-primary">Off:</strong> every member of this organization can see
            and edit every org project.
          </p>
          <p>
            <strong className="font-medium text-text-primary">On:</strong> members see only the projects they own or
            reach through a project group. A reader grant is read-only; members can change only the projects they own
            or where a group makes them an editor or admin. Org admins and owners still see and change everything.
            Syncing a session from a project’s repository no longer gives access to a restricted project.
          </p>
        </div>
        <p className="text-xs text-text-tertiary">
          Currently <strong className="font-medium text-text-secondary">{restricted ? 'on' : 'off'}</strong>.
          {!canEdit && ' Only org owners and admins can change this.'}
        </p>
        {canEdit && upgradeBlocked && (
          <Alert variant="info" title="Available on Team and Enterprise">
            {UPGRADE_MESSAGE} Upgrade to restrict who can see each project.
          </Alert>
        )}
        {canEdit && inlineError && !isUpgradeRequired(update.error) && (
          <Alert variant="danger">Couldn’t change project visibility: {groupErrorMessage(update.error)}</Alert>
        )}
      </div>

      <ConfirmDialog
        open={pending === true}
        title="Restrict project visibility?"
        confirmLabel="Restrict visibility"
        pending={update.isPending}
        error={update.isError ? groupErrorMessage(update.error) : null}
        onConfirm={() => apply(true)}
        onClose={() => setPending(null)}
      >
        <p>
          Members will see only the projects they own or that a project group grants them. Members who rely on
          org-wide access will lose it until an admin adds their projects to a group and grants them access.
        </p>
        <p>
          A reader grant is read-only: members can change only the projects they own or where a group makes them an
          editor or admin. Org admins and owners keep full access to every project. This change is recorded in the
          org audit trail.
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={pending === false}
        title="Open projects to every member?"
        confirmLabel="Turn off restriction"
        pending={update.isPending}
        error={update.isError ? groupErrorMessage(update.error) : null}
        onConfirm={() => apply(false)}
        onClose={() => setPending(null)}
      >
        <p>
          Every member of this organization will be able to see and edit every org project again, whatever their
          group access. This change is recorded in the org audit trail.
        </p>
      </ConfirmDialog>
    </section>
  );
}
