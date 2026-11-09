import { useState } from 'react';
import type { ApprovalPolicy } from '../api/client';
import {
  Alert,
  Button,
  Checkbox,
  Dialog,
  DialogFooter,
  DialogHeader,
  Input,
  Select,
  Switch,
  Textarea,
} from '../components/ui';
import { useSaveApprovalPolicy } from '../hooks/useApprovals';
import { errorMessage } from '../utils/errorMessage';
import { ENTRY_TYPES, HARMS, PRIORITIES, TARGET_LABEL } from './format';
import {
  buildPolicyBody,
  emptyForm,
  formFromPolicy,
  hasErrors,
  MAX_REQUIREMENTS,
  newRow,
  validateForm,
  type PolicyFormErrors,
  type PolicyFormState,
  type RequirementRow,
} from './policyForm';

export interface PickerProject {
  id: string;
  name: string;
}
export interface PickerMember {
  user_id: string;
  label: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  orgId: string;
  /** The active version being edited; omit to create. */
  policy?: ApprovalPolicy;
  projects: PickerProject[];
  members: PickerMember[];
}

/** Create or edit a policy. An edit saves a new version of the same policy. */
export function PolicyDialog(props: Props) {
  // Remount the form whenever the dialog opens so state never leaks between uses.
  return props.open ? <PolicyDialogBody {...props} /> : null;
}

function toggle(list: string[], v: string): string[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

function PolicyDialogBody({ onClose, orgId, policy, projects, members }: Props) {
  const save = useSaveApprovalPolicy(orgId);
  const [s, setS] = useState<PolicyFormState>(() => (policy ? formFromPolicy(policy) : emptyForm()));
  const [errors, setErrors] = useState<PolicyFormErrors>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const editing = !!policy;
  const set = (patch: Partial<PolicyFormState>) => setS((cur) => ({ ...cur, ...patch }));
  const setRow = (key: number, patch: Partial<RequirementRow>) =>
    set({ requirements: s.requirements.map((r) => (r.key === key ? { ...r, ...patch } : r)) });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const errs = validateForm(s);
    setErrors(errs);
    setServerError(null);
    if (hasErrors(errs)) return;
    save.mutate(
      { policyId: policy?.id, body: buildPolicyBody(s) },
      { onSuccess: onClose, onError: (err) => setServerError(errorMessage(err)) },
    );
  }

  const titleId = 'approval-policy-title';
  return (
    <Dialog open onClose={onClose} titleId={titleId} className="max-w-2xl">
      <DialogHeader titleId={titleId}>{editing ? 'Edit approval policy' : 'New approval policy'}</DialogHeader>
      <form onSubmit={submit} noValidate className="space-y-5">
        {editing && (
          <p className="text-xs text-text-tertiary">
            Saving creates version {(policy?.version ?? 0) + 1}. Requests already raised stay on the version that
            created them.
          </p>
        )}
        <Input
          title="Name"
          value={s.name}
          maxLength={120}
          error={errors.name}
          onChange={(e) => set({ name: e.target.value })}
        />
        <Textarea
          title="Description"
          value={s.description}
          rows={2}
          maxLength={2000}
          onChange={(e) => set({ description: e.target.value })}
        />
        <Select
          title="Applies to"
          value={s.targetKind}
          disabled={editing}
          onValueChange={(v) => set({ targetKind: v as PolicyFormState['targetKind'] })}
          options={[
            { value: 'merge', label: TARGET_LABEL.merge },
            { value: 'kb_promote', label: TARGET_LABEL.kb_promote },
          ]}
          hint={editing ? 'The target cannot change. Create a new policy instead.' : undefined}
        />
        <Switch
          checked={s.blocking}
          onChange={(blocking) => set({ blocking })}
          label={s.blocking ? 'Blocking: work waits until approved' : 'Advisory: shows a warning only'}
        />

        <fieldset className="space-y-3">
          <legend className="text-sm font-medium text-text-primary">When it applies</legend>
          <p className="text-xs text-text-tertiary">Leave a field empty to match everything.</p>
          <div role="group" aria-label="Projects" className="space-y-1">
            <p className="text-sm font-medium text-text-secondary">Projects</p>
            {projects.length === 0 ? (
              <p className="text-xs text-text-tertiary">No organization projects yet. The policy covers every project.</p>
            ) : (
              <div className="grid gap-1 sm:grid-cols-2">
                {projects.map((p) => (
                  <Checkbox
                    key={p.id}
                    label={p.name}
                    checked={s.projectIds.includes(p.id)}
                    onChange={() => set({ projectIds: toggle(s.projectIds, p.id) })}
                  />
                ))}
              </div>
            )}
          </div>
          {s.targetKind === 'merge' ? (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <Select
                  title="Priority at least"
                  value={s.priority}
                  onValueChange={(v) => set({ priority: v as PolicyFormState['priority'] })}
                  options={[{ value: '', label: 'Any priority' }, ...PRIORITIES.map((p) => ({ value: p, label: p }))]}
                />
                <Select
                  title="Ticket kind"
                  value={s.ticketKind}
                  onValueChange={(v) => set({ ticketKind: v as PolicyFormState['ticketKind'] })}
                  options={[
                    { value: '', label: 'Tasks and issues' },
                    { value: 'task', label: 'Tasks' },
                    { value: 'issue', label: 'Issues' },
                  ]}
                />
              </div>
              <div role="group" aria-label="Finding harm" className="space-y-1">
                <p className="text-sm font-medium text-text-secondary">Any finding with harm</p>
                <div className="grid gap-1 sm:grid-cols-2">
                  {HARMS.map((h) => (
                    <Checkbox
                      key={h.value}
                      label={h.label}
                      checked={s.harms.includes(h.value)}
                      onChange={() => set({ harms: toggle(s.harms, h.value) })}
                    />
                  ))}
                </div>
              </div>
              <Input
                title="Checklist classes"
                value={s.checklistClasses}
                error={errors.checklistClasses}
                hint="Comma separated, for example auth, payments."
                onChange={(e) => set({ checklistClasses: e.target.value })}
              />
            </>
          ) : (
            <>
              <div role="group" aria-label="Entry types" className="space-y-1">
                <p className="text-sm font-medium text-text-secondary">Entry types</p>
                <div className="grid gap-1 sm:grid-cols-3">
                  {ENTRY_TYPES.map((t) => (
                    <Checkbox
                      key={t}
                      label={t}
                      checked={s.entryTypes.includes(t)}
                      onChange={() => set({ entryTypes: toggle(s.entryTypes, t) })}
                    />
                  ))}
                </div>
              </div>
              <Textarea
                title="Entity ref prefixes"
                value={s.entityRefPrefixes}
                rows={2}
                error={errors.entityRefPrefixes}
                hint="One per line, for example docs/compliance/."
                onChange={(e) => set({ entityRefPrefixes: e.target.value })}
              />
            </>
          )}
        </fieldset>

        <fieldset className="space-y-3">
          <legend className="text-sm font-medium text-text-primary">Who must approve</legend>
          {errors.requirements && <p role="alert" className="text-xs text-danger">{errors.requirements}</p>}
          {s.requirements.map((r, i) => (
            <RequirementEditor
              key={r.key}
              index={i}
              row={r}
              members={members}
              error={errors.rows?.[r.key]}
              canRemove={s.requirements.length > 1}
              onChange={(patch) => setRow(r.key, patch)}
              onRemove={() => set({ requirements: s.requirements.filter((x) => x.key !== r.key) })}
            />
          ))}
          <Button
            type="button"
            size="sm"
            variant="secondary"
            disabled={s.requirements.length >= MAX_REQUIREMENTS}
            disabledReason={`A policy can have at most ${MAX_REQUIREMENTS} requirements`}
            onClick={() => set({ requirements: [...s.requirements, newRow()] })}
          >
            Add requirement
          </Button>
          <p className="text-xs text-text-tertiary">Every requirement must be met. Approvers cannot approve their own work.</p>
        </fieldset>

        {serverError && <Alert variant="danger">{serverError}</Alert>}
        <DialogFooter>
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            {editing ? 'Save new version' : 'Create policy'}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

function RequirementEditor({
  index,
  row,
  members,
  error,
  canRemove,
  onChange,
  onRemove,
}: {
  index: number;
  row: RequirementRow;
  members: PickerMember[];
  error?: string;
  canRemove: boolean;
  onChange: (patch: Partial<RequirementRow>) => void;
  onRemove: () => void;
}) {
  const n = index + 1;
  return (
    <div role="group" aria-label={`Requirement ${n}`} className="space-y-3 rounded-lg border border-border bg-surface p-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <Select
          title="Approver"
          value={row.who}
          onValueChange={(v) => onChange({ who: v as RequirementRow['who'] })}
          options={[
            { value: 'org_role', label: 'By org role' },
            { value: 'users', label: 'Named people' },
          ]}
        />
        {row.who === 'org_role' && (
          <Select
            title="Role"
            value={row.role}
            onValueChange={(v) => onChange({ role: v as RequirementRow['role'] })}
            options={[
              { value: 'admin', label: 'Admin or owner' },
              { value: 'owner', label: 'Owner' },
            ]}
          />
        )}
        <Input
          title="Approvals needed"
          type="number"
          min={1}
          value={row.count}
          onChange={(e) => onChange({ count: e.target.value })}
        />
      </div>
      {row.who === 'users' && (
        <div role="group" aria-label={`People for requirement ${n}`} className="space-y-1">
          <p className="text-sm font-medium text-text-secondary">People</p>
          {members.length === 0 ? (
            <p className="text-xs text-text-tertiary">No members loaded.</p>
          ) : (
            <div className="grid gap-1 sm:grid-cols-2">
              {members.map((m) => (
                <Checkbox
                  key={m.user_id}
                  label={m.label}
                  checked={row.userIds.includes(m.user_id)}
                  onChange={() => onChange({ userIds: toggle(row.userIds, m.user_id) })}
                />
              ))}
            </div>
          )}
        </div>
      )}
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      {canRemove && (
        <Button type="button" size="sm" variant="ghost" onClick={onRemove} aria-label={`Remove requirement ${n}`}>
          Remove
        </Button>
      )}
    </div>
  );
}
