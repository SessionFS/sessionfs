/**
 * Settings → Assistant: personal model settings and instructions for the
 * session helper, plus this month's token usage. The settings apply to turns
 * on the user's own key; on the org key the org's settings apply (said
 * explicitly when the status reports the org key). On a plan without the
 * helper the API answers 403 and the section shows a short Enterprise note.
 */

import { Link } from 'react-router-dom';

import { Alert, Button, Card } from '../components/ui';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import {
  InstructionsField,
  ModelSettingsFields,
  SubsectionHeading,
  UsageTotals,
} from '../helper/AssistantSettingsFields';
import { LIMITS, fieldErrorsFrom, isUpgradeRequired, type Original } from '../helper/assistantSettings';
import { useAssistantForm } from '../helper/useAssistantForm';
import { useHelperStatus } from '../helper/useHelper';
import { useSaveUserHelperSettings, useUserHelperSettings } from '../helper/useHelperSettings';
import type { UserHelperSettings } from '../api/client';

const linkClass =
  'font-medium text-brand underline-offset-2 hover:underline outline-none focus-visible:rounded-sm focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]';

function originalOf(data: UserHelperSettings | undefined): Original | null {
  if (!data) return null;
  return { settings: data.settings, instructions: data.instructions };
}

export default function AssistantSection() {
  const { data, isLoading, isError, error, refetch } = useUserHelperSettings();
  const gated = isError && isUpgradeRequired(error);
  // Which key a turn would use decides whether these settings apply.
  const status = useHelperStatus(!gated && !!data);

  const header = (
    <>
      <h2 className="mb-1 text-lg font-semibold text-text-primary">Assistant</h2>
      <p className="mb-5 text-sm text-text-tertiary">
        How the session helper answers you: model settings, your own instructions and this month&rsquo;s usage.
      </p>
    </>
  );

  if (isLoading) {
    return (
      <Card level="elevated" className="p-5">
        {header}
        <p className="py-2 text-sm text-text-tertiary">Loading assistant settings…</p>
      </Card>
    );
  }

  if (gated) {
    return (
      <Card level="elevated" className="p-5">
        {header}
        <Alert variant="info" title="Available on Enterprise">
          <p>
            The session helper answers questions across your sessions and project knowledge, with links to every
            source. Its settings are available on the Enterprise plan.{' '}
            <Link to="/settings/billing" className={linkClass}>
              View plans
            </Link>
          </p>
        </Alert>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card level="elevated" className="p-5">
        {header}
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
      </Card>
    );
  }

  return (
    <Card level="elevated" className="p-5">
      {header}
      <PersonalForm data={data} keySource={status.data?.key_source} />
    </Card>
  );
}

function PersonalForm({ data, keySource }: { data: UserHelperSettings; keySource: string | null | undefined }) {
  const { addToast } = useToast();
  const save = useSaveUserHelperSettings();
  const form = useAssistantForm(originalOf(data), { org: false, instructionsLimit: LIMITS.personalInstructions });
  const { draft, errors } = form;
  if (!draft) return null;

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

  return (
    <form
      className="space-y-7"
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        handleSave();
      }}
    >
      {keySource === 'org' && (
        <Alert variant="info" title="You're using your org's key">
          Your answers currently run on the organization&rsquo;s key, so the org&rsquo;s model settings apply. These
          settings take effect when you add your own key. Your instructions always apply.
        </Alert>
      )}
      {keySource === null && (
        <Alert variant="info" title="No model key yet">
          <p>
            Add a model key in{' '}
            <Link to="/settings/data-protection" className={linkClass}>
              Data protection
            </Link>{' '}
            to use the session helper. You can set these up first.
          </p>
        </Alert>
      )}

      <div>
        <SubsectionHeading level={3} description="Used for your turns on your own model key.">Model settings</SubsectionHeading>
        <ModelSettingsFields
          idPrefix="assistant"
          draft={draft}
          errors={errors}
          resets={form.resets}
          onChange={form.change}
          onReset={form.resetField}
          disabled={save.isPending}
        />
      </div>

      <div>
        <SubsectionHeading level={3} description="Added after your organization's instructions on every turn.">
          Personal instructions
        </SubsectionHeading>
        <InstructionsField
          id="assistant-instructions"
          label="Instructions for the assistant"
          value={draft.instructions}
          onChange={(v) => form.change({ instructions: v })}
          limit={LIMITS.personalInstructions}
          error={errors.instructions}
          disabled={save.isPending}
          placeholder="e.g. Answer in British English. Show file paths relative to the repo root."
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={!form.dirty || save.isPending} loading={save.isPending}>
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
        <SubsectionHeading level={3} description="Your turns on any key, this calendar month (UTC).">Your usage this month</SubsectionHeading>
        <UsageTotals usage={data.usage} />
      </div>
    </form>
  );
}
