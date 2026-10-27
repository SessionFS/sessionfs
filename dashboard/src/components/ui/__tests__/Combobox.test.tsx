import { useEffect, useState } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Combobox, Dialog, DialogHeader, type ComboboxOption } from '../';
import { axe } from '../../../test/axe';

const MANY: ComboboxOption[] = Array.from({ length: 120 }, (_, i) => {
  const key = `P${String(i).padStart(3, '0')}`;
  return { value: key, label: `${key} · Project ${i}` };
});

function Harness({
  options = MANY,
  initial = '',
  onChange = () => {},
  ...rest
}: Partial<React.ComponentProps<typeof Combobox>> & {
  initial?: string;
  onChange?: (v: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <Combobox
      title="Project"
      placeholder="Pick one"
      value={value}
      onValueChange={(v) => {
        setValue(v);
        onChange(v);
      }}
      options={options}
      {...rest}
    />
  );
}

describe('Combobox', () => {
  it('opens with a search box and every option in a height-bounded scroll container', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Project' }));

    const search = screen.getByRole('combobox', { name: 'Search project' });
    expect(search).toHaveFocus();
    expect(screen.getAllByRole('option')).toHaveLength(120);

    const popup = screen.getByTestId('combobox-popup');
    expect(popup.style.maxHeight).toBe('360px');
    const list = screen.getByRole('listbox', { name: 'Project' });
    expect(list).toHaveClass('overflow-y-auto');
    expect(list).toHaveClass('min-h-0');
  });

  it('filters client-side by label and by value, and says when nothing matches', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Project' }));
    const search = screen.getByRole('combobox');

    await user.type(search, 'project 11');
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      'P011 · Project 11',
      'P110 · Project 110',
      'P111 · Project 111',
      'P112 · Project 112',
      'P113 · Project 113',
      'P114 · Project 114',
      'P115 · Project 115',
      'P116 · Project 116',
      'P117 · Project 117',
      'P118 · Project 118',
      'P119 · Project 119',
    ]);

    await user.clear(search);
    await user.type(search, 'p099');
    expect(screen.getAllByRole('option')).toHaveLength(1);

    await user.clear(search);
    await user.type(search, 'zzz');
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('No matches for “zzz”');
  });

  it('selects with the keyboard: arrows, Home/End and Enter', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Harness onChange={onChange} />);
    const trigger = screen.getByRole('button', { name: 'Project' });
    trigger.focus();
    await user.keyboard('{ArrowDown}');
    const search = screen.getByRole('combobox');

    await user.keyboard('{ArrowDown}{ArrowDown}');
    expect(search).toHaveAttribute('aria-activedescendant', expect.stringMatching(/-opt-1$/));
    await user.keyboard('{End}');
    expect(search).toHaveAttribute('aria-activedescendant', expect.stringMatching(/-opt-119$/));
    await user.keyboard('{Home}{ArrowDown}{Enter}');

    expect(onChange).toHaveBeenCalledWith('P001');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveTextContent('P001 · Project 1');
    expect(trigger).toHaveFocus();
  });

  it('typing makes the first match active so Enter picks it', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<Harness onChange={onChange} />);
    await user.click(screen.getByRole('button', { name: 'Project' }));
    await user.type(screen.getByRole('combobox'), 'project 42{Enter}');
    expect(onChange).toHaveBeenCalledWith('P042');
  });

  it('shows the empty message when there are no options at all', async () => {
    const user = userEvent.setup();
    render(<Harness options={[]} emptyMessage="Nothing to map yet" />);
    await user.click(screen.getByRole('button', { name: 'Project' }));
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Nothing to map yet');
  });

  it('leaves filtering to the caller when onSearchChange is set, and shows the footer', async () => {
    const onSearch = vi.fn();
    const user = userEvent.setup();
    render(
      <Harness
        options={MANY.slice(0, 3)}
        onSearchChange={onSearch}
        footer="Showing the first 3 projects — type to search"
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Project' }));
    expect(screen.getByRole('status')).toHaveTextContent('Showing the first 3 projects — type to search');
    await user.type(screen.getByRole('combobox'), 'zzz');
    expect(onSearch).toHaveBeenLastCalledWith('zzz');
    // Server-filtered: the given options stay as they are.
    expect(screen.getAllByRole('option')).toHaveLength(3);
  });

  it('restarts from the first match when server results replace the list', async () => {
    const user = userEvent.setup();
    function ServerHarness() {
      const [q, setQ] = useState('');
      const [shown, setShown] = useState('');
      // Results land a moment after the keystroke, like a debounced fetch.
      useEffect(() => {
        const t = setTimeout(() => setShown(q), 30);
        return () => clearTimeout(t);
      }, [q]);
      const options = shown ? MANY.filter((o) => o.value.endsWith('7')) : MANY.slice(0, 5);
      return <Harness options={options} onSearchChange={setQ} />;
    }
    render(<ServerHarness />);
    await user.click(screen.getByRole('button', { name: 'Project' }));
    const search = screen.getByRole('combobox');
    await user.type(search, '7');
    // Still the old page: arrow down to its third row.
    await user.keyboard('{ArrowDown}{ArrowDown}');
    expect(search).toHaveAttribute('aria-activedescendant', expect.stringMatching(/-opt-2$/));
    await waitFor(() => expect(screen.getAllByRole('option')[0]).toHaveTextContent('P007'));
    expect(search).toHaveAttribute('aria-activedescendant', expect.stringMatching(/-opt-0$/));
  });

  it('ignores Enter and clicks while server results are still for an older query', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    function SlowServer() {
      const [q, setQ] = useState('');
      const [shown, setShown] = useState('');
      useEffect(() => {
        const t = setTimeout(() => setShown(q), 80);
        return () => clearTimeout(t);
      }, [q]);
      const options = shown ? MANY.filter((o) => o.label.endsWith(`Project ${shown}`)) : MANY.slice(0, 5);
      return <Harness options={options} onSearchChange={setQ} pending={q !== shown} onChange={onChange} />;
    }
    render(<SlowServer />);
    await user.click(screen.getByRole('button', { name: 'Project' }));
    const search = screen.getByRole('combobox');

    // Type and press Enter at once: the list still shows the old page.
    await user.type(search, '42{Enter}');
    expect(onChange).not.toHaveBeenCalled();
    expect(search).not.toHaveAttribute('aria-activedescendant');
    expect(screen.getByRole('listbox')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('listbox')).toHaveClass('opacity-60');
    expect(screen.getByRole('status')).toHaveTextContent('Searching…');
    await user.click(screen.getAllByRole('option')[0]);
    expect(onChange).not.toHaveBeenCalled();

    // Once the results for "42" arrive, Enter picks the first match.
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(1));
    expect(search).toHaveAttribute('aria-activedescendant', expect.stringMatching(/-opt-0$/));
    await user.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledWith('P042');
  });

  it('never grows taller than the room the viewport leaves', async () => {
    const user = userEvent.setup();
    const original = window.innerHeight;
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 300 });
    try {
      render(<Harness />);
      const trigger = screen.getByRole('button', { name: 'Project' });
      vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
        top: 100, bottom: 136, left: 0, right: 300, width: 300, height: 36, x: 0, y: 100, toJSON: () => ({}),
      } as DOMRect);
      await user.click(trigger);
      // 300 - 136 - 4 (gap) - 12 (margin) = 148px below, more than above.
      expect(screen.getByTestId('combobox-popup').style.maxHeight).toBe('148px');
    } finally {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: original });
    }
  });

  it('shows the loading state', async () => {
    const user = userEvent.setup();
    render(<Harness options={[]} onSearchChange={() => {}} loading />);
    await user.click(screen.getByRole('button', { name: 'Project' }));
    expect(screen.getByRole('status')).toHaveTextContent('Searching…');
  });

  it('keeps a selection label that is not on the current page', () => {
    render(<Harness options={[]} initial="ZULU" selectedLabel="ZULU · Zulu team" />);
    expect(screen.getByRole('button', { name: 'Project' })).toHaveTextContent('ZULU · Zulu team');
  });

  it('Escape inside a dialog closes the popup, not the dialog', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Dialog open onClose={onClose} titleId="t">
        <DialogHeader titleId="t">Pick</DialogHeader>
        <Harness />
      </Dialog>,
    );
    await user.click(screen.getByRole('button', { name: 'Project' }));
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Project' })).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on outside click', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Harness />
        <p>outside</p>
      </div>,
    );
    await user.click(screen.getByRole('button', { name: 'Project' }));
    await user.click(screen.getByText('outside'));
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument());
  });

  it('has no axe violations open or closed', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <form>
        <Harness options={MANY.slice(0, 5)} footer="Showing the first 5" />
      </form>,
    );
    expect(await axe(container)).toHaveNoViolations();
    await user.click(screen.getByRole('button', { name: 'Project' }));
    await user.keyboard('{ArrowDown}');
    expect(await axe(container)).toHaveNoViolations();
    await user.type(screen.getByRole('combobox'), 'nothing-matches');
    expect(await axe(container)).toHaveNoViolations();
  });
});
