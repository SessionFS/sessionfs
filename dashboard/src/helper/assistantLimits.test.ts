import { describe, expect, it } from 'vitest';

import { DEFAULTS, buildPatch, describeSettings, draftFrom, type Original } from './assistantSettings';
import { stopReasonRetryable, stopReasonText } from './format';

const BASE: Original = {
  settings: { ...DEFAULTS },
  instructions: null,
};

const OPTS = { org: false, instructionsLimit: 2000 };

describe('per-question limits (GitHub #143)', () => {
  it('defaults to 25 tool calls, 3 failures in a row and 150,000 input tokens', () => {
    expect(DEFAULTS).toMatchObject({ max_tool_calls: 25, max_consecutive_tool_failures: 3, max_input_tokens: 150_000 });
  });

  it('fills the defaults in when an older server does not send the limits', () => {
    const older = { ...DEFAULTS };
    delete older.max_tool_calls;
    delete older.max_consecutive_tool_failures;
    delete older.max_input_tokens;
    const original: Original = { settings: older, instructions: null };
    const draft = draftFrom(original);
    expect(draft.max_tool_calls).toBe('25');
    expect(draft.max_consecutive_tool_failures).toBe('3');
    expect(draft.max_input_tokens).toBe('150000');
    // Unchanged defaults are not sent.
    expect(buildPatch(original, draft, new Set(), OPTS).body).toEqual({});
  });

  it('sends only the limits that changed, accepting grouped digits', () => {
    const draft = { ...draftFrom(BASE), max_tool_calls: '40', max_input_tokens: '300,000' };
    const { body, errors } = buildPatch(BASE, draft, new Set(), OPTS);
    expect(errors).toEqual({});
    expect(body.settings).toEqual({ max_tool_calls: 40, max_input_tokens: 300_000 });
  });

  it('refuses values outside the bounds', () => {
    const draft = {
      ...draftFrom(BASE),
      max_tool_calls: '101',
      max_consecutive_tool_failures: '0',
      max_input_tokens: '5000',
    };
    const { errors } = buildPatch(BASE, draft, new Set(), OPTS);
    expect(Object.keys(errors).sort()).toEqual(['max_consecutive_tool_failures', 'max_input_tokens', 'max_tool_calls']);
  });

  it('a reset sends an explicit null', () => {
    const original: Original = { settings: { ...DEFAULTS, max_tool_calls: 60 }, instructions: null };
    const { body } = buildPatch(original, draftFrom(original), new Set(['max_tool_calls'] as const), OPTS);
    expect(body.settings).toEqual({ max_tool_calls: null });
  });

  it('org ceilings are optional and validated', () => {
    const original: Original = {
      settings: { ...DEFAULTS, ceiling_max_output_tokens: null, ceiling_max_tool_steps: null },
      instructions: null,
      monthly_token_budget: null,
    };
    const draft = { ...draftFrom(original), ceiling_max_tool_calls: '30', ceiling_max_input_tokens: '2000000' };
    const { body, errors } = buildPatch(original, draft, new Set(), { org: true, instructionsLimit: 4000 });
    expect(errors).toEqual({ ceiling_max_input_tokens: 'Enter a number from 10,000 to 1,000,000.' });
    expect(body.settings).toEqual({ ceiling_max_tool_calls: 30 });
  });

  it('lists the limits among the effective settings', () => {
    const labels = describeSettings(DEFAULTS).map((r) => `${r.label}: ${r.value}`);
    expect(labels).toContain('Max tool calls per question: 25');
    expect(labels).toContain('Max input tokens per question: 150,000');
  });
});

describe('stop reasons', () => {
  it('has a plain sentence for early stops and none for a normal end', () => {
    expect(stopReasonText('stop')).toBeNull();
    expect(stopReasonText(undefined)).toBeNull();
    expect(stopReasonText('input_token_cap')).toBe('Stopped early: this question reached its input token limit.');
    expect(stopReasonText('missing_capability')).toMatch(/no tool for/);
  });

  it('offers Try again only where asking again can help', () => {
    expect(stopReasonRetryable('tool_failures')).toBe(true);
    expect(stopReasonRetryable('missing_capability')).toBe(false);
    expect(stopReasonRetryable('stop')).toBe(false);
  });
});
