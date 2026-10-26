/**
 * Token usage under each answer, the "Monthly budget reached" error, and the
 * pure assistant-settings helpers (patch builder, 422 mapping, formatting).
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { axe } from '../test/axe';
import { AssistantMessage, ErrorCard } from './HelperMessage';
import { buildPatch, draftFrom, fieldErrorsFrom, isUpgradeRequired, type Original } from './assistantSettings';
import { classifyError, compactTokens, streamError, usageFrom, type Turn } from './format';

vi.mock('../hooks/useProjects', () => ({ useProject: () => ({ data: undefined }) }));

const actions = { onNewConversation: () => {}, onNavigate: () => {} };

function turn(extra: Partial<Turn>): Turn {
  return { id: 't1', role: 'assistant', content: 'The retry came from a late ack.', toolSteps: [], citations: [], status: 'done', ...extra };
}

const BUDGET_BODY = JSON.stringify({
  error: {
    code: 'helper_budget_exceeded',
    message: "This org's monthly session-helper token budget has been reached.",
    details: { error: 'helper_budget_exceeded' },
  },
});

describe('usage line', () => {
  it('shows compact in/out tokens under a completed answer, with full numbers for screen readers', async () => {
    const { container } = render(
      <MemoryRouter>
        <AssistantMessage turn={turn({ usage: { input: 1234, output: 260 } })} actions={actions} />
      </MemoryRouter>,
    );
    expect(screen.getByText('1.2k in · 260 out')).toBeInTheDocument();
    expect(screen.getByText('1,234 input tokens, 260 output tokens')).toHaveClass('sr-only');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('is hidden while streaming, after a stop, and when no tokens were reported', () => {
    const { rerender } = render(
      <MemoryRouter>
        <AssistantMessage turn={turn({ status: 'streaming', usage: { input: 10, output: 5 } })} actions={actions} />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/ in · /)).toBeNull();
    rerender(
      <MemoryRouter>
        <AssistantMessage turn={turn({ status: 'stopped', usage: { input: 10, output: 5 } })} actions={actions} />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/ in · /)).toBeNull();
    rerender(
      <MemoryRouter>
        <AssistantMessage turn={turn({})} actions={actions} />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/ in · /)).toBeNull();
  });

  it('reads tokens from the done event and stored messages', () => {
    expect(usageFrom({ input_tokens: 1200, output_tokens: 260 })).toEqual({ input: 1200, output: 260 });
    expect(usageFrom({ usage: { input_tokens: 5, output_tokens: 0 } })).toEqual({ input: 5, output: 0 });
    expect(usageFrom({ input_tokens: 0, output_tokens: 0 })).toBeUndefined();
  });

  it('formats token counts compactly', () => {
    expect(compactTokens(950)).toBe('950');
    expect(compactTokens(1234)).toBe('1.2k');
    expect(compactTokens(1000)).toBe('1k');
    expect(compactTokens(12_345)).toBe('12k');
    expect(compactTokens(999_999)).toBe('1M');
    expect(compactTokens(2_400_000)).toBe('2.4M');
  });
});

describe('monthly budget reached', () => {
  it('classifies 429 helper_budget_exceeded (HTTP and stream) as a budget error', () => {
    expect(classifyError(new ApiError(429, BUDGET_BODY)).kind).toBe('budget');
    expect(streamError('helper_budget_exceeded', 'reached').kind).toBe('budget');
    // A plain rate limit is not a budget.
    expect(classifyError(new ApiError(429, 'Too Many Requests')).kind).toBe('other');
  });

  it('explains the pause and points a member at their own key, with no Retry', async () => {
    const onRetry = vi.fn();
    const { container } = render(
      <MemoryRouter>
        <ErrorCard error={classifyError(new ApiError(429, BUDGET_BODY))} actions={{ ...actions, onRetry }} />
      </MemoryRouter>,
    );
    expect(screen.getByText('Monthly budget reached')).toBeInTheDocument();
    expect(screen.getByText(/resets on the 1st \(UTC\)/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add your own key' })).toHaveAttribute('href', '/settings/data-protection');
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('sends org admins to the org settings to raise the budget', () => {
    render(
      <MemoryRouter>
        <ErrorCard error={{ kind: 'budget', message: 'reached' }} actions={{ ...actions, isOrgAdmin: true }} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'Open org settings' })).toHaveAttribute(
      'href',
      '/settings/organization?tab=policies',
    );
  });
});

describe('assistant settings patch', () => {
  const original: Original = {
    settings: {
      max_output_tokens: 8000,
      temperature: 0.3,
      top_p: null,
      max_tool_steps: 6,
      reasoning_effort: 'low',
      answer_style: 'concise',
      ceiling_max_output_tokens: 3000,
      ceiling_max_tool_steps: null,
    },
    instructions: 'Hi',
    monthly_token_budget: null,
  };
  const opts = { org: true, instructionsLimit: 4000 };

  it('is empty when nothing changed', () => {
    expect(buildPatch(original, draftFrom(original), new Set(), opts)).toEqual({ body: {}, errors: {} });
  });

  it('clears nullable values and the instructions with null', () => {
    const d = { ...draftFrom(original), reasoning_effort: '' as const, ceiling_max_output_tokens: '', instructions: '  ' };
    expect(buildPatch(original, d, new Set(), opts).body).toEqual({
      settings: { reasoning_effort: null, ceiling_max_output_tokens: null },
      instructions: null,
    });
  });

  it('reports client-side bounds errors instead of a body change', () => {
    const d = { ...draftFrom(original), max_tool_steps: '25', top_p: '1.5', monthly_token_budget: '-3' };
    const { errors } = buildPatch(original, d, new Set(), opts);
    expect(Object.keys(errors).sort()).toEqual(['max_tool_steps', 'monthly_token_budget', 'top_p']);
  });

  it('never clears a value from unparseable text, and accepts digit grouping', () => {
    for (const bad of ['1e6', '+5', '5,0,', '1.5', 'abc', '-1']) {
      const d = { ...draftFrom(original), monthly_token_budget: bad, ceiling_max_output_tokens: bad };
      const { body, errors } = buildPatch(original, d, new Set(), opts);
      expect(body.monthly_token_budget).toBeUndefined();
      expect(body.settings?.ceiling_max_output_tokens).toBeUndefined();
      expect(errors.monthly_token_budget).toBeTruthy();
      expect(errors.ceiling_max_output_tokens).toBeTruthy();
    }
    const grouped = { ...draftFrom(original), monthly_token_budget: '6,000,000', max_output_tokens: '4_096' };
    expect(buildPatch(original, grouped, new Set(), opts).body).toEqual({
      settings: { max_output_tokens: 4096 },
      monthly_token_budget: 6_000_000,
    });
  });

  it('maps pydantic 422s to fields, including FastAPI default shape', () => {
    const env = new ApiError(
      422,
      JSON.stringify({ error: { code: '422', message: 'Validation error', details: { errors: [{ loc: ['body', 'monthly_token_budget'], msg: 'Input should be greater than or equal to 0' }] } } }),
    );
    expect(fieldErrorsFrom(env)).toEqual({ monthly_token_budget: 'Must be greater than or equal to 0.' });
    const plain = new ApiError(422, JSON.stringify({ detail: [{ loc: ['body', 'settings', 'top_p'], msg: 'bad' }] }));
    expect(fieldErrorsFrom(plain)).toEqual({ top_p: 'bad' });
    expect(fieldErrorsFrom(new ApiError(500, 'x'))).toBeNull();
  });

  it('recognises the plan gate', () => {
    expect(isUpgradeRequired(new ApiError(403, JSON.stringify({ error: { code: 'upgrade_required', message: 'm' } })))).toBe(true);
    expect(isUpgradeRequired(new ApiError(403, JSON.stringify({ error: { code: 'service_key_not_allowed', message: 'm' } })))).toBe(false);
    expect(isUpgradeRequired(new ApiError(404, 'x'))).toBe(false);
  });
});
