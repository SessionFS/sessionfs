/**
 * Org Policies → Assistant: how the session helper answers on the org key.
 *
 * Owners/admins edit the model settings, the ceilings for members on the org
 * key, the monthly token budget and the org instructions, and see this
 * month's usage per member. Members see the effective values and their own
 * usage, read-only. Saving sends only the changed fields (the server merges);
 * "Reset to default" sends an explicit null for that field.
 */

import { useMemo } from 'react';

import type { OrgHelperSettings } from '../api/client';
import { Alert, Button, Input, TableShell } from '../components/ui';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import {
  BudgetMeter,
  BudgetPercent,
  EffectiveSettingsList,
  InstructionsField,
  ModelSettingsFields,
  SubsectionHeading,
  UsageTotals,
} from '../helper/AssistantSettingsFields';
import { LIMITS, fieldErrorsFrom, type Original } from '../helper/assistantSettings';
import { compactTokens } from '../helper/format';
import { useAssistantForm } from '../helper/useAssistantForm';
import { useOrgHelperSettings, useSaveOrgHelperSettings } from '../helper/useHelperSettings';
import { useOrgMembers } from './useOrgMembers';

const HEADING_ID = 'org-assistant-heading';

function isAdminView(data: OrgHelperSettings | undefined): data is OrgHelperSettings & Required<Pick<OrgHelperSettings, 'settings'>> {
  return !!data && data.settings !== undefined && (data.role === 'admin' || data.role === 'owner');
}

function originalOf(data: OrgHelperSettings | undefined): Original | null {
  if (!isAdminView(data)) return null;
  return {
    settings: data.settings,
    instructions: data.instructions ?? null,
    monthly_token_budget: data.monthly_token_budget ?? null,
  };
}

export default function OrgAssistantSection({ orgId }: { orgId: string }) {
  const { data, isLoading, isError, error, refetch } = useOrgHelperSettings(orgId);

  const heading = (
    <div className="mb-4">
      <h3 id={HEADING_ID} className="text-base font-semibold text-text-primary">
        Assistant
      </h3>
      <p className="mt-1 text-sm text-text-secondary">
        How the session helper answers for everyone on the org key: model settings, limits, instructions and token
        usage.
      </p>
    </div>
  );

  let body;
  if (isLoading) {
    body = <p className="py-2 text-sm text-text-tertiary">Loading assistant settings…</p>;
  } else if (isError || !data) {
    body = (
      <Alert
        variant="danger"
        title="Couldn't load the assistant settings"
        action={
          <Button size="sm" variant="secondary" onClick={() => void refetch()}>
            Retry
          </Button>
        }
      >
        {errorMessage(error)}
      </Alert>
    );
  } else if (isAdminView(data)) {
    body = <AdminView orgId={orgId} data={data} />;
  } else {
    body = <MemberView data={data} />;
  }

  return (
    <section aria-labelledby={HEADING_ID}>
      {heading}
      {body}
    </section>
  );
}

function PlanNote({ admin }: { admin: boolean }) {
  return (
    <Alert variant="info" title="Available on Enterprise">
      {admin
        ? "The session helper is part of the Enterprise plan. Stored settings are kept but can't be changed on the current plan."
        : 'The session helper is part of the Enterprise plan.'}
    </Alert>
  );
}

// ── Member: read-only ──

function MemberView({ data }: { data: OrgHelperSettings }) {
  if (!data.plan_includes_helper) return <PlanNote admin={false} />;
  return (
    <div className="space-y-6">
      {data.budget_reached && (
        <Alert variant="warning" title="Monthly budget reached">
          Answers on the org key are paused until the 1st (UTC). Ask an org admin to raise the budget, or add your own
          model key to keep going.
        </Alert>
      )}
      <div>
        <SubsectionHeading id="org-assistant-effective" description="Set by your org admins. They apply when you use the org key.">
          Model settings
        </SubsectionHeading>
        <EffectiveSettingsList settings={data.effective_settings} labelledBy="org-assistant-effective" />
        <p className="mt-3 text-sm text-text-secondary">
          {data.has_instructions
            ? 'Your org has added instructions for the assistant.'
            : 'Your org has not added instructions for the assistant.'}
        </p>
      </div>
      <div>
        <SubsectionHeading description="Your turns on the org key, this calendar month (UTC).">Your usage this month</SubsectionHeading>
        <UsageTotals usage={data.usage} />
        {!data.budget_reached && data.budget_used_pct != null && (
          <div className="mt-3 rounded-lg border border-border bg-bg-sunken px-3 py-2.5">
            <BudgetPercent pct={data.budget_used_pct} />
          </div>
        )}
      </div>
    </div>
  );
}

// ── Admin: edit ──

function AdminView({ orgId, data }: { orgId: string; data: OrgHelperSettings }) {
  const { addToast } = useToast();
  const save = useSaveOrgHelperSettings(orgId);
  const members = useOrgMembers(orgId);
  const original = originalOf(data);
  const form = useAssistantForm(original, { org: true, instructionsLimit: LIMITS.orgInstructions });
  const editable = data.plan_includes_helper;
  const disabled = !editable || save.isPending;

  const budget = data.monthly_token_budget ?? null;
  const used = data.usage.total_tokens;

  const nameOf = useMemo(() => {
    const map = new Map<string, { name: string; email: string | null }>();
    for (const m of members.data?.members ?? []) {
      map.set(m.user_id, { name: m.display_name || m.email, email: m.display_name ? m.email : null });
    }
    return map;
  }, [members.data]);

  const perMember = [...(data.usage.per_member ?? [])].sort((a, b) => b.total_tokens - a.total_tokens);

  function handleSave() {
    const body = form.validate();
    if (!body) return;
    save.mutate(body, {
      onSuccess: (resp) => {
        const next = originalOf(resp);
        if (next) form.load(next);
        addToast('success', 'Assistant settings saved');
      },
      onError: (err) => {
        const fields = fieldErrorsFrom(err);
        if (fields) form.setErrors(fields);
        else addToast('error', `Save failed: ${errorMessage(err)}`);
      },
    });
  }

  const { draft, errors } = form;
  if (!draft) return null;

  return (
    <form
      className="space-y-7"
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        handleSave();
      }}
    >
      {!editable && <PlanNote admin />}

      <div>
        <SubsectionHeading description="Used for every turn on the org key. Members' personal keys use their own settings.">
          Model settings
        </SubsectionHeading>
        <ModelSettingsFields
          idPrefix="org-assistant"
          draft={draft}
          errors={errors}
          resets={form.resets}
          onChange={form.change}
          onReset={form.resetField}
          disabled={disabled}
        />
      </div>

      <div>
        <SubsectionHeading description="Caps for members using the org key. Leave empty for no ceiling.">
          Ceilings and budget
        </SubsectionHeading>
        <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2">
          <Input
            id="org-assistant-ceiling-output"
            title="Output token ceiling"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={draft.ceiling_max_output_tokens}
            placeholder="No ceiling"
            onChange={(e) => form.change({ ceiling_max_output_tokens: e.target.value })}
            hint={`${LIMITS.outputTokens.min}–${LIMITS.outputTokens.max.toLocaleString()}. Caps max output tokens above.`}
            error={errors.ceiling_max_output_tokens}
            disabled={disabled}
          />
          <Input
            id="org-assistant-ceiling-steps"
            title="Tool step ceiling"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={draft.ceiling_max_tool_steps}
            placeholder="No ceiling"
            onChange={(e) => form.change({ ceiling_max_tool_steps: e.target.value })}
            hint={`${LIMITS.toolSteps.min}–${LIMITS.toolSteps.max}. Caps max tool steps above.`}
            error={errors.ceiling_max_tool_steps}
            disabled={disabled}
          />
          <Input
            id="org-assistant-budget"
            title="Monthly token budget"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            value={draft.monthly_token_budget}
            placeholder="Unlimited"
            onChange={(e) => form.change({ monthly_token_budget: e.target.value })}
            hint="Input + output tokens per UTC month on the org key. Leave empty for unlimited."
            error={errors.monthly_token_budget}
            disabled={disabled}
          />
          <div className="flex flex-col justify-center rounded-lg border border-border bg-bg-sunken px-3 py-2.5">
            {budget !== null ? (
              <BudgetMeter used={used} budget={budget} />
            ) : (
              <p className="text-sm text-text-secondary">
                No budget set. <span className="tabular-nums text-text-primary">{compactTokens(used)}</span> tokens used
                this month.
              </p>
            )}
          </div>
        </div>
      </div>

      <div>
        <SubsectionHeading description="Added to every turn that uses the org key or asks about this org's projects.">
          Org instructions
        </SubsectionHeading>
        <InstructionsField
          id="org-assistant-instructions"
          label="Instructions for the assistant"
          value={draft.instructions}
          onChange={(v) => form.change({ instructions: v })}
          limit={LIMITS.orgInstructions}
          error={errors.instructions}
          disabled={disabled}
          placeholder="e.g. Cite the newest session first. Use our ticket ids when you mention work items."
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={!editable || !form.dirty || save.isPending} loading={save.isPending}>
          Save changes
        </Button>
        {form.dirty && (
          <Button type="button" variant="ghost" onClick={form.discard} disabled={save.isPending}>
            Discard
          </Button>
        )}
        {form.dirty && <span className="text-xs text-text-tertiary">Unsaved changes</span>}
      </div>

      <div className="border-t border-border pt-6">
        <SubsectionHeading id="org-assistant-usage" description="Turns on the org key this calendar month (UTC).">
          Usage this month
        </SubsectionHeading>
        <UsageTotals usage={data.usage} />
        {perMember.length === 0 ? (
          <p className="mt-3 text-sm text-text-tertiary">No one has used the org key this month.</p>
        ) : (
          <TableShell bordered wrapperClassName="mt-3" className="tabular-nums">
            <caption className="sr-only">Token usage per member this month</caption>
            <thead>
              <tr className="border-b border-border text-2xs uppercase tracking-wide text-text-tertiary">
                <th scope="col" className="w-1/2 px-3 py-2 font-semibold">Member</th>
                <th scope="col" className="hidden px-3 py-2 text-right font-semibold sm:table-cell">Input</th>
                <th scope="col" className="hidden px-3 py-2 text-right font-semibold sm:table-cell">Output</th>
                <th scope="col" className="px-3 py-2 text-right font-semibold">Total</th>
              </tr>
            </thead>
            <tbody>
              {perMember.map((m) => {
                const who = nameOf.get(m.user_id);
                return (
                  <tr key={m.user_id} className="border-b border-border last:border-b-0">
                    <th scope="row" className="max-w-0 px-3 py-2 text-left font-normal">
                      {who ? (
                        <>
                          <span className="block truncate text-text-primary">{who.name}</span>
                          {who.email && <span className="block truncate text-xs text-text-tertiary">{who.email}</span>}
                        </>
                      ) : members.isLoading ? (
                        <span className="block truncate font-mono text-xs text-text-tertiary">{m.user_id}</span>
                      ) : (
                        <>
                          <span className="block text-text-secondary">Former member</span>
                          <span className="block truncate font-mono text-xs text-text-tertiary">{m.user_id}</span>
                        </>
                      )}
                    </th>
                    <td className="hidden px-3 py-2 text-right text-text-secondary sm:table-cell" title={m.input_tokens.toLocaleString()}>
                      {compactTokens(m.input_tokens)}
                    </td>
                    <td className="hidden px-3 py-2 text-right text-text-secondary sm:table-cell" title={m.output_tokens.toLocaleString()}>
                      {compactTokens(m.output_tokens)}
                    </td>
                    <td className="px-3 py-2 text-right font-medium text-text-primary" title={m.total_tokens.toLocaleString()}>
                      {compactTokens(m.total_tokens)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </TableShell>
        )}
      </div>
    </form>
  );
}
