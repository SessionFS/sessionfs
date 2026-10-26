import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ModelFilter } from './ModelFilter';
import type { SessionModelCount } from '../../api/client';
import { axe } from '../../test/axe';

const models = [
  { model_id: 'gpt-5-20250127', count: 3 },
  { model_id: 'claude-sonnet-4-6', count: 1 },
  { model_id: null, count: 2 },
];

/** Holds the selection like SessionList does (via the URL) so checked state
 *  and re-renders behave as in the app. */
function Stateful({
  initial = [],
  list = models,
  onChange = () => {},
}: {
  initial?: string[];
  list?: SessionModelCount[];
  onChange?: (v: string[]) => void;
}) {
  const [selected, setSelected] = useState<string[]>(initial);
  return (
    <ModelFilter
      models={list}
      selected={selected}
      onChange={(v) => {
        setSelected(v);
        onChange(v);
      }}
    />
  );
}

function optionRow(name: RegExp) {
  return screen.getByRole('checkbox', { name }).closest('div') as HTMLElement;
}

describe('ModelFilter', () => {
  it('renders a friendly label and count badge for each model, with an Unknown model bucket', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ModelFilter models={models} selected={[]} onChange={onChange} />);

    await user.click(screen.getByRole('button', { name: 'Filter by model' }));

    // Friendly labels strip the trailing date stamp; null becomes "Unknown model".
    expect(screen.getByRole('checkbox', { name: /gpt-5/ })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /claude-sonnet-4-6/ })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Unknown model/ })).toBeInTheDocument();
    // Count badges are surfaced next to each option.
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    // The raw id is reachable as visually hidden text in the option's name.
    expect(screen.getByRole('checkbox', { name: /gpt-5-20250127/ })).toBeInTheDocument();
  });

  it('toggles selection without closing and reports the raw id (or unknown sentinel)', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Stateful onChange={onChange} />);

    const trigger = screen.getByRole('button', { name: 'Filter by model' });
    await user.click(trigger);
    await user.click(screen.getByRole('checkbox', { name: /gpt-5/ }));
    expect(onChange).toHaveBeenLastCalledWith(['gpt-5-20250127']);

    // Still open after a toggle: the dialog and its checkboxes stay visible.
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('dialog', { name: 'Filter by model' })).toBeVisible();
    expect(screen.getByRole('checkbox', { name: /gpt-5/ })).toBeChecked();

    await user.click(screen.getByRole('checkbox', { name: /Unknown model/ }));
    expect(onChange).toHaveBeenLastCalledWith(['gpt-5-20250127', 'unknown']);
    expect(screen.getByRole('dialog', { name: 'Filter by model' })).toBeVisible();
    expect(screen.getByRole('checkbox', { name: /Unknown model/ })).toBeChecked();
  });

  it('shows selected models missing from the list with a 0 count so they can be unticked', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    // e.g. a shared link, or a model whose sessions are all in another tool
    render(<Stateful initial={['retired-model-x', 'gpt-5-20250127']} onChange={onChange} />);

    expect(screen.getByRole('button', { name: 'Filter by model' })).toHaveTextContent('2 models');
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));

    const stale = screen.getByRole('checkbox', { name: /retired-model-x/ });
    expect(stale).toBeChecked();
    expect(optionRow(/retired-model-x/)).toHaveTextContent('0');

    await user.click(stale);
    expect(onChange).toHaveBeenLastCalledWith(['gpt-5-20250127']);
    // Once unticked it is in neither the selection nor /models, so it drops out
    expect(screen.queryByRole('checkbox', { name: /retired-model-x/ })).not.toBeInTheDocument();
  });

  it('shows a selected unknown bucket even when /models has none (count 0)', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Stateful
        initial={['unknown']}
        list={[{ model_id: 'claude-sonnet-4-6', count: 4 }]}
        onChange={onChange}
      />,
    );
    expect(screen.getByRole('button', { name: 'Filter by model' })).toHaveTextContent('Unknown model');
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    const unknown = screen.getByRole('checkbox', { name: /Unknown model/ });
    expect(unknown).toBeChecked();
    expect(optionRow(/Unknown model/)).toHaveTextContent('0');
    // No duplicate unknown entry
    expect(screen.getAllByRole('checkbox', { name: /Unknown model/ })).toHaveLength(1);
    await user.click(unknown);
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it('keeps selections when the list changes (another Tool filter) and shows them at 0', async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <ModelFilter models={models} selected={['claude-sonnet-4-6']} onChange={() => {}} />,
    );
    rerender(
      <ModelFilter models={[{ model_id: 'codex-4-1', count: 5 }]} selected={['claude-sonnet-4-6']} onChange={() => {}} />,
    );
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    expect(screen.getByRole('checkbox', { name: /claude-sonnet-4-6/ })).toBeChecked();
    expect(optionRow(/claude-sonnet-4-6/)).toHaveTextContent('0');
    expect(screen.getByRole('checkbox', { name: /codex-4-1/ })).not.toBeChecked();
  });

  it('offers "Clear models" when something is selected', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Stateful initial={['gpt-5-20250127', 'unknown']} onChange={onChange} />);
    const trigger = screen.getByRole('button', { name: 'Filter by model' });
    await user.click(trigger);
    await user.click(screen.getByRole('button', { name: 'Clear models' }));
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(trigger).toHaveTextContent('All Models');
    expect(trigger).toHaveFocus();
  });

  it('has no "Clear models" item when nothing is selected', async () => {
    const user = userEvent.setup();
    render(<ModelFilter models={models} selected={[]} onChange={() => {}} />);
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    expect(screen.queryByRole('button', { name: 'Clear models' })).not.toBeInTheDocument();
  });

  it('uses dialog disclosure semantics with aria-controls, and Escape returns focus to the trigger', async () => {
    const user = userEvent.setup();
    render(<ModelFilter models={models} selected={[]} onChange={() => {}} />);
    const trigger = screen.getByRole('button', { name: 'Filter by model' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    const controls = trigger.getAttribute('aria-controls');
    expect(controls).toBeTruthy();

    await user.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Filter by model' });
    expect(dialog.id).toBe(controls);
    expect(screen.getByRole('group', { name: 'Models' })).toBeInTheDocument();
    // Focus moves into the dialog
    expect(screen.getByRole('checkbox', { name: /gpt-5/ })).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('shows a loading line instead of the empty message while /models loads', async () => {
    const user = userEvent.setup();
    render(<ModelFilter models={[]} selected={[]} onChange={() => {}} loading />);
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    expect(screen.getByText(/Loading models/)).toBeInTheDocument();
    expect(screen.queryByText('No models yet')).not.toBeInTheDocument();
  });

  it('reports a load error but keeps a stale selection clearable', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ModelFilter models={[]} selected={['gpt-5']} onChange={onChange} error />);
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    expect(screen.getByText(/Couldn.t load models/)).toBeInTheDocument();
    expect(screen.queryByText('No models yet')).not.toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: /gpt-5/ }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it('has no axe violations', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <ModelFilter models={models} selected={['gpt-5-20250127', 'gone-model']} onChange={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
    await user.click(screen.getByRole('button', { name: 'Filter by model' }));
    expect(await axe(container)).toHaveNoViolations();
  });
});
