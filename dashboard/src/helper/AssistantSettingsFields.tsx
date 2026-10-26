/**
 * Shared pieces of the assistant settings forms (org Policies tab and
 * Settings → Assistant): the model settings fields, the instructions field
 * with its live character counter, the read-only effective values, and the
 * usage summary / budget meter.
 */

import { useId, type ReactNode } from 'react';

import type { HelperModelSettings, HelperTokenUsage } from '../api/client';
import { Button, Checkbox, Input, Select, Textarea } from '../components/ui';
import {
  DEFAULTS,
  LIMITS,
  REASONING_LABELS,
  STYLE_LABELS,
  charCount,
  describeSettings,
  type Draft,
  type FieldErrors,
  type ResettableKey,
} from './assistantSettings';
import { compactTokens } from './format';

// ── Layout ──

export function SubsectionHeading({
  id,
  children,
  description,
  level = 4,
}: {
  id?: string;
  children: ReactNode;
  description?: ReactNode;
  level?: 3 | 4;
}) {
  const H = level === 3 ? 'h3' : 'h4';
  return (
    <div className="mb-3">
      <H id={id} className="text-sm font-semibold text-text-primary">
        {children}
      </H>
      {description && <p className="mt-0.5 text-xs text-text-tertiary">{description}</p>}
    </div>
  );
}

function ResetButton({
  field,
  label,
  draftValue,
  defaultValue,
  pending,
  onReset,
  disabled,
}: {
  field: ResettableKey;
  label: string;
  draftValue: string;
  defaultValue: string;
  pending: boolean;
  onReset: (key: ResettableKey) => void;
  disabled?: boolean;
}) {
  if (pending) {
    return <p className="text-xs text-text-tertiary">Resets to the default ({defaultValue}) when you save.</p>;
  }
  if (draftValue === String(DEFAULTS[field])) return null;
  return (
    <div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="-ml-3"
        onClick={() => onReset(field)}
        disabled={disabled}
        aria-label={`Reset ${label.toLowerCase()} to default`}
      >
        Reset to default
      </Button>
    </div>
  );
}

// ── Model settings ──

export interface ModelSettingsFieldsProps {
  draft: Draft;
  errors: FieldErrors;
  resets: ReadonlySet<ResettableKey>;
  onChange: (patch: Partial<Draft>) => void;
  onReset: (key: ResettableKey) => void;
  disabled?: boolean;
  /** Prefix for element ids so two forms can share a page. */
  idPrefix: string;
}

function parseTemperature(raw: string): number | null {
  const t = raw.trim();
  if (!/^(\d+(\.\d*)?|\.\d+)$/.test(t)) return null;
  const n = Number(t);
  return n >= LIMITS.temperature.min && n <= LIMITS.temperature.max ? n : null;
}

export function ModelSettingsFields({ draft, errors, resets, onChange, onReset, disabled, idPrefix }: ModelSettingsFieldsProps) {
  const id = (k: string) => `${idPrefix}-${k}`;
  // The slider mirrors the number only while it holds a valid temperature;
  // otherwise it sits disabled and neutral rather than showing a different value.
  const sliderValue = parseTemperature(draft.temperature);

  return (
    <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2">
      <div className="flex flex-col gap-1">
        <Input
          id={id('max-output-tokens')}
          title="Max output tokens"
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={draft.max_output_tokens}
          onChange={(e) => onChange({ max_output_tokens: e.target.value })}
          hint={`${LIMITS.outputTokens.min}–${LIMITS.outputTokens.max.toLocaleString()} tokens per answer. Default ${DEFAULTS.max_output_tokens.toLocaleString()}.`}
          error={errors.max_output_tokens}
          disabled={disabled}
        />
        <ResetButton
          field="max_output_tokens"
          label="Max output tokens"
          draftValue={draft.max_output_tokens}
          defaultValue={DEFAULTS.max_output_tokens.toLocaleString()}
          pending={resets.has('max_output_tokens')}
          onReset={onReset}
          disabled={disabled}
        />
      </div>

      <div className="flex flex-col gap-1">
        <Input
          id={id('max-tool-steps')}
          title="Max tool steps"
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={draft.max_tool_steps}
          onChange={(e) => onChange({ max_tool_steps: e.target.value })}
          hint={`${LIMITS.toolSteps.min}–${LIMITS.toolSteps.max} rounds of tool calls before the answer. Default ${DEFAULTS.max_tool_steps}.`}
          error={errors.max_tool_steps}
          disabled={disabled}
        />
        <ResetButton
          field="max_tool_steps"
          label="Max tool steps"
          draftValue={draft.max_tool_steps}
          defaultValue={String(DEFAULTS.max_tool_steps)}
          pending={resets.has('max_tool_steps')}
          onReset={onReset}
          disabled={disabled}
        />
      </div>

      <div className="flex flex-col gap-2">
        <Input
          id={id('temperature')}
          title="Temperature"
          type="text"
          inputMode="decimal"
          autoComplete="off"
          value={draft.temperatureProviderDefault ? '' : draft.temperature}
          placeholder={draft.temperatureProviderDefault ? 'Provider default' : undefined}
          onChange={(e) => onChange({ temperature: e.target.value })}
          hint="0–2. Lower is more focused and repeatable. Default 0."
          error={errors.temperature}
          disabled={disabled || draft.temperatureProviderDefault}
        />
        <input
          type="range"
          min={LIMITS.temperature.min}
          max={LIMITS.temperature.max}
          step="any"
          value={sliderValue ?? LIMITS.temperature.min}
          onChange={(e) => onChange({ temperature: e.target.value })}
          disabled={disabled || draft.temperatureProviderDefault || sliderValue === null}
          aria-label="Temperature slider"
          aria-valuetext={sliderValue === null ? 'Not set' : undefined}
          aria-describedby={`${id('temperature')}-hint`}
          className={`h-4 w-full accent-[var(--brand)] disabled:cursor-not-allowed ${
            sliderValue === null ? 'opacity-30 grayscale' : 'cursor-pointer disabled:opacity-50'
          }`}
        />
        <Checkbox
          id={id('temperature-default')}
          label="Provider default"
          description="Don't send a temperature; the provider picks one."
          checked={draft.temperatureProviderDefault}
          onChange={(e) =>
            onChange({
              temperatureProviderDefault: e.target.checked,
              // Coming back from provider default starts at the default value.
              ...(!e.target.checked && draft.temperature.trim() === '' ? { temperature: String(DEFAULTS.temperature) } : {}),
            })
          }
          disabled={disabled}
        />
      </div>

      <Input
        id={id('top-p')}
        title="Top P (optional)"
        type="text"
        inputMode="decimal"
        autoComplete="off"
        value={draft.top_p}
        placeholder="Not sent"
        onChange={(e) => onChange({ top_p: e.target.value })}
        hint="0–1. Leave empty to not send it. On Anthropic models, setting Top P drops temperature."
        error={errors.top_p}
        disabled={disabled}
      />

      <Select
        id={id('reasoning-effort')}
        title="Reasoning effort"
        value={draft.reasoning_effort}
        onValueChange={(v) => onChange({ reasoning_effort: v as Draft['reasoning_effort'] })}
        options={Object.entries(REASONING_LABELS).map(([value, label]) => ({ value, label }))}
        hint="Only reasoning models use this; others ignore it."
        error={errors.reasoning_effort}
        disabled={disabled}
      />

      <div className="flex flex-col gap-1">
        <Select
          id={id('answer-style')}
          title="Answer style"
          value={draft.answer_style}
          onValueChange={(v) => onChange({ answer_style: v as Draft['answer_style'] })}
          options={Object.entries(STYLE_LABELS).map(([value, label]) => ({ value, label }))}
          hint="Concise answers lead with the result; detailed ones walk through the reasoning."
          error={errors.answer_style}
          disabled={disabled}
        />
        <ResetButton
          field="answer_style"
          label="Answer style"
          draftValue={draft.answer_style}
          defaultValue={STYLE_LABELS[DEFAULTS.answer_style]}
          pending={resets.has('answer_style')}
          onReset={onReset}
          disabled={disabled}
        />
      </div>
    </div>
  );
}

// ── Instructions ──

export function InstructionsField({
  id,
  label,
  value,
  onChange,
  limit,
  error,
  disabled,
  placeholder,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  limit: number;
  error?: string;
  disabled?: boolean;
  placeholder?: string;
}) {
  // Code points, matching the server's len().
  const count = charCount(value);
  const over = count > limit;
  const near = !over && count >= limit * 0.9;
  return (
    <div>
      <Textarea
        id={id}
        title={label}
        rows={5}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        error={error}
        hint={
          <span className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
            <span className="min-w-0 flex-1 basis-56">
              Instructions shape tone and focus. They can&rsquo;t override access limits, citations or
              confirm-before-acting.
            </span>{' '}
            <span className={`shrink-0 tabular-nums ${over ? 'font-medium text-danger' : near ? 'text-warning' : ''}`}>
              {count.toLocaleString()} / {limit.toLocaleString()}{' '}
              <span className="sr-only">characters</span>
            </span>
          </span>
        }
      />
      {/* Announce only crossing the limit, never each keystroke. */}
      <span className="sr-only" role="status" aria-live="polite">
        {over ? `Over the ${limit.toLocaleString()}-character limit.` : ''}
      </span>
    </div>
  );
}

// ── Read-only effective values ──

export function EffectiveSettingsList({ settings, labelledBy }: { settings: HelperModelSettings; labelledBy?: string }) {
  return (
    <dl aria-labelledby={labelledBy} className="grid gap-x-5 gap-y-3 sm:grid-cols-2">
      {describeSettings(settings).map((row) => (
        <div key={row.label} className="flex items-baseline justify-between gap-3 border-b border-border pb-2">
          <dt className="text-sm text-text-secondary">{row.label}</dt>
          <dd className="text-sm font-medium tabular-nums text-text-primary">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

// ── Usage ──

export function UsageTotals({ usage }: { usage: HelperTokenUsage }) {
  const items = [
    { label: 'Input', value: usage.input_tokens },
    { label: 'Output', value: usage.output_tokens },
    { label: 'Total', value: usage.total_tokens },
  ];
  return (
    <dl className="grid grid-cols-3 gap-2">
      {items.map((it) => (
        <div key={it.label} className="rounded-lg border border-border bg-bg-sunken px-3 py-2">
          <dt className="text-2xs font-semibold uppercase tracking-wide text-text-tertiary">{it.label}</dt>
          <dd className="mt-0.5 text-base font-semibold tabular-nums text-text-primary" title={`${it.value.toLocaleString()} tokens`}>
            {compactTokens(it.value)}
            <span className="sr-only"> tokens ({it.value.toLocaleString()})</span>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** "used / budget" with a bar; colour shifts at 80% and 100%. */
export function BudgetMeter({ used, budget }: { used: number; budget: number }) {
  const labelId = useId();
  const pct = budget > 0 ? Math.min(100, (used / budget) * 100) : 100;
  const reached = used >= budget;
  const tone = reached ? 'bg-danger' : pct >= 80 ? 'bg-warning' : 'bg-brand';
  return (
    <div className="flex flex-col gap-1.5">
      <p id={labelId} className="flex flex-wrap items-baseline justify-between gap-x-3 text-sm">
        <span className="text-text-secondary">Used this month</span>
        <span className="tabular-nums text-text-primary">
          <span className="font-semibold">{compactTokens(used)}</span>
          <span className="text-text-tertiary"> / {compactTokens(budget)} tokens</span>
          <span className="sr-only"> ({used.toLocaleString()} of {budget.toLocaleString()})</span>
        </span>
      </p>
      <div
        role="progressbar"
        aria-labelledby={labelId}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
        aria-valuetext={`${Math.round((budget > 0 ? used / budget : 1) * 100)}% of the monthly budget`}
        className="h-2 w-full overflow-hidden rounded-full bg-bg-sunken"
      >
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
      {reached && (
        <p className="text-xs text-danger">
          Budget reached. Turns on the org key are refused until the budget resets on the 1st (UTC) or you raise it.
        </p>
      )}
    </div>
  );
}

/** Members' view of the org budget: a percentage only, never the amount. */
export function BudgetPercent({ pct }: { pct: number }) {
  const labelId = useId();
  const clamped = Math.max(0, Math.min(100, pct));
  const tone = clamped >= 80 ? 'bg-warning' : 'bg-brand';
  return (
    <div className="flex flex-col gap-1.5">
      <p id={labelId} className="flex flex-wrap items-baseline justify-between gap-x-3 text-sm">
        <span className="text-text-secondary">Org budget used this month</span>
        <span className="font-semibold tabular-nums text-text-primary">{clamped}%</span>
      </p>
      <div
        role="progressbar"
        aria-labelledby={labelId}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={clamped}
        className="h-2 w-full overflow-hidden rounded-full bg-bg-sunken"
      >
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${clamped}%` }} />
      </div>
    </div>
  );
}
