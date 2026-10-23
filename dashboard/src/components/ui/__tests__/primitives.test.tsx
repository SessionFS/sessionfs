import React, { useState } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Button, Card, Input, Textarea, Select, Dialog, DialogHeader, DialogFooter, Tabs, TabPanel, Table, Dropdown, Tooltip, Kbd, KbdShortcut, PageHeader, PageContainer } from '../';

// ── Button ──

describe('Button', () => {
  it('renders children', () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('applies variant classes', () => {
    const { rerender } = render(<Button variant="primary">A</Button>);
    expect(screen.getByRole('button')).toHaveClass('bg-brand');

    rerender(<Button variant="secondary">B</Button>);
    expect(screen.getByRole('button')).toHaveClass('border');

    rerender(<Button variant="ghost">C</Button>);
    expect(screen.getByRole('button')).toHaveClass('bg-transparent');

    rerender(<Button variant="danger">D</Button>);
    expect(screen.getByRole('button')).toHaveClass('bg-[var(--danger-solid)]');
  });

  it('disables while loading and shows spinner', () => {
    render(<Button loading>Loading</Button>);
    const btn = screen.getByRole('button');
    expect(btn).toBeDisabled();
    expect(btn.querySelector('svg')).toBeTruthy(); // spinner
  });

  it('supports disabled prop', () => {
    render(<Button disabled>Nope</Button>);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('fires onClick', async () => {
    const fn = vi.fn();
    render(<Button onClick={fn}>Click</Button>);
    await userEvent.click(screen.getByRole('button'));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('does not fire onClick when disabled', async () => {
    const fn = vi.fn();
    render(<Button disabled onClick={fn}>Nope</Button>);
    await userEvent.click(screen.getByRole('button'));
    expect(fn).not.toHaveBeenCalled();
  });

  it('accepts size prop', () => {
    const { rerender } = render(<Button size="sm">S</Button>);
    expect(screen.getByRole('button')).toHaveClass('text-xs');

    rerender(<Button size="md">M</Button>);
    expect(screen.getByRole('button')).toHaveClass('text-sm');
  });
});

// ── Card ──

describe('Card', () => {
  it('renders children', () => {
    render(<Card>Hello</Card>);
    expect(screen.getByText('Hello')).toBeInTheDocument();
  });

  it('uses surface bg by default', () => {
    render(<Card data-testid="c">X</Card>);
    expect(screen.getByTestId('c').style.backgroundColor).toBe('var(--surface)');
  });

  it('uses elevated bg for level=elevated', () => {
    render(<Card level="elevated" data-testid="c">X</Card>);
    expect(screen.getByTestId('c').style.backgroundColor).toBe('var(--bg-elevated)');
  });

  it('applies toolEdge as 3px left border', () => {
    render(<Card toolEdge="var(--tool-claude)" data-testid="c">X</Card>);
    const el = screen.getByTestId('c');
    expect(el.style.borderLeftWidth).toBe('3px');
    expect(el.style.borderLeftColor).toBe('var(--tool-claude)');
  });

  it('applies topEdge as 3px top border', () => {
    render(<Card topEdge="var(--brand)" data-testid="c">X</Card>);
    const el = screen.getByTestId('c');
    expect(el.style.borderTopWidth).toBe('3px');
    expect(el.style.borderTopColor).toBe('var(--brand)');
  });
});

// ── Input / Textarea / Select ──

describe('Input', () => {
  it('renders an input', () => {
    render(<Input placeholder="Type here" />);
    expect(screen.getByPlaceholderText('Type here')).toBeInTheDocument();
  });

  it('shows error state', () => {
    render(<Input error="Required" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Required');
  });

  it('associates label with input via htmlFor when id matches label slug (legacy compat)', () => {
    // Simulates TicketsTab: id="field-description" + title="Description (markdown)"
    render(<Input id="field-description" title="Description (markdown)" />);
    const input = screen.getByLabelText('Description (markdown)');
    expect(input).toBeInTheDocument();
    expect(input.id).toBe('field-description');
  });

  it('associates label with input via auto-generated id when no explicit id', () => {
    render(<Input title="Email address" />);
    const input = screen.getByLabelText('Email address');
    expect(input).toBeInTheDocument();
    // The auto-generated id should be non-empty and applied to the control
    expect(input.id).toBeTruthy();
  });
});

describe('Textarea', () => {
  it('renders a textarea', () => {
    render(<Textarea placeholder="Message" />);
    expect(screen.getByPlaceholderText('Message').tagName).toBe('TEXTAREA');
  });

  it('shows error state', () => {
    render(<Textarea error="Too short" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Too short');
  });

  it('associates label with textarea via htmlFor with explicit id', () => {
    render(<Textarea id="field-notes" title="Session notes" />);
    const textarea = screen.getByLabelText('Session notes');
    expect(textarea).toBeInTheDocument();
    expect(textarea.id).toBe('field-notes');
  });

  it('associates label with textarea via auto-generated id when no explicit id', () => {
    render(<Textarea title="Comments" />);
    const textarea = screen.getByLabelText('Comments');
    expect(textarea).toBeInTheDocument();
    expect(textarea.id).toBeTruthy();
  });
});

describe('Select', () => {
  const opts = [
    { value: 'a', label: 'Alpha' },
    { value: 'b', label: 'Beta' },
  ];

  it('renders select trigger', () => {
    render(<Select value="" onValueChange={vi.fn()} options={opts} />);
    expect(screen.getByRole('button')).toBeInTheDocument();
  });

  it('opens panel on click and shows options', async () => {
    render(<Select value="" onValueChange={vi.fn()} options={opts} />);
    await userEvent.click(screen.getByRole('button'));
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Alpha' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Beta' })).toBeInTheDocument();
  });

  it('calls onValueChange when option is clicked', async () => {
    const onChange = vi.fn();
    render(<Select value="a" onValueChange={onChange} options={opts} />);
    // Open the panel
    fireEvent.click(screen.getByRole('button'));
    // Options are <li role="option"> (no nested focusable button); click directly.
    fireEvent.click(screen.getByRole('option', { name: 'Beta' }));
    expect(onChange).toHaveBeenCalledWith('b');
  });

  it('shows selected value as trigger text', () => {
    render(<Select value="b" onValueChange={vi.fn()} options={opts} />);
    expect(screen.getByRole('button')).toHaveTextContent('Beta');
  });

  it('shows placeholder when no value selected', () => {
    render(
      <Select
        value=""
        onValueChange={vi.fn()}
        options={opts}
        placeholder="Pick one"
      />,
    );
    expect(screen.getByRole('button')).toHaveTextContent('Pick one');
  });

  it('shows error state', () => {
    render(<Select value="" onValueChange={vi.fn()} options={opts} error="Pick one" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Pick one');
  });

  it('closes on Escape', async () => {
    render(<Select value="" onValueChange={vi.fn()} options={opts} />);
    await userEvent.click(screen.getByRole('button'));
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });
  });

  it('supports keyboard navigation with ArrowDown / Enter', async () => {
    const onChange = vi.fn();
    render(<Select value="" onValueChange={onChange} options={opts} />);
    await userEvent.click(screen.getByRole('button'));
    // ArrowDown to first option
    await userEvent.keyboard('{ArrowDown}');
    await userEvent.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledWith('a');
  });

  it('associates label with select via htmlFor with explicit id', () => {
    render(
      <Select
        id="field-assign-to"
        title="Assign to persona"
        value=""
        onValueChange={vi.fn()}
        options={opts}
      />,
    );
    const select = screen.getByLabelText('Assign to persona');
    expect(select).toBeInTheDocument();
    expect(select.id).toBe('field-assign-to');
  });

  it('associates label with select via auto-generated id when no explicit id', () => {
    render(
      <Select
        title="Priority"
        value=""
        onValueChange={vi.fn()}
        options={opts}
      />,
    );
    const select = screen.getByLabelText('Priority');
    expect(select).toBeInTheDocument();
    expect(select.id).toBeTruthy();
  });

  it('has aria-haspopup and aria-expanded', async () => {
    render(<Select value="" onValueChange={vi.fn()} options={opts} />);
    const trigger = screen.getByRole('button');
    expect(trigger).toHaveAttribute('aria-haspopup', 'listbox');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('marks selected option with aria-selected', async () => {
    render(<Select value="a" onValueChange={vi.fn()} options={opts} />);
    await userEvent.click(screen.getByRole('button'));
    expect(screen.getByRole('option', { name: 'Alpha' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('option', { name: 'Beta' })).toHaveAttribute('aria-selected', 'false');
  });
});

// ── Dialog ──

describe('Dialog', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <Dialog open={false} onClose={vi.fn()} titleId="t1">
        <p>hidden</p>
      </Dialog>,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders content when open', () => {
    render(
      <Dialog open onClose={vi.fn()} titleId="t1">
        <DialogHeader titleId="t1">Title</DialogHeader>
        <p>Body</p>
        <DialogFooter><button>OK</button></DialogFooter>
      </Dialog>,
    );
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Title')).toBeInTheDocument();
    expect(screen.getByText('Body')).toBeInTheDocument();
    expect(screen.getByText('OK')).toBeInTheDocument();
  });

  it('sets aria-modal and aria-labelledby', () => {
    render(
      <Dialog open onClose={vi.fn()} titleId="my-title">
        <DialogHeader titleId="my-title">X</DialogHeader>
      </Dialog>,
    );
    const d = screen.getByRole('dialog');
    expect(d).toHaveAttribute('aria-modal', 'true');
    expect(d).toHaveAttribute('aria-labelledby', 'my-title');
  });

  it('closes on Escape', async () => {
    const onClose = vi.fn();
    render(
      <Dialog open onClose={onClose} titleId="t1">
        <DialogHeader titleId="t1">X</DialogHeader>
      </Dialog>,
    );
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on backdrop click', () => {
    const onClose = vi.fn();
    const { container } = render(
      <Dialog open onClose={onClose} titleId="t1">
        <DialogHeader titleId="t1">X</DialogHeader>
      </Dialog>,
    );
    // Backdrop is the first child div
    const backdrop = container.querySelector('.absolute.inset-0');
    fireEvent.click(backdrop!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

// ── Tabs ──

describe('Tabs', () => {
  const tabs = [
    { key: 'one', label: 'Tab One', content: <p>Content 1</p> },
    { key: 'two', label: 'Tab Two', content: <p>Content 2</p> },
  ];

  it('renders tab buttons with role=tab', () => {
    render(<Tabs tabs={tabs} activeKey="one" onChange={vi.fn()} />);
    expect(screen.getByRole('tab', { name: 'Tab One' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Tab Two' })).toBeInTheDocument();
  });

  it('shows active tab content', () => {
    render(<Tabs tabs={tabs} activeKey="one" onChange={vi.fn()} />);
    expect(screen.getByText('Content 1')).toBeInTheDocument();
    expect(screen.queryByText('Content 2')).not.toBeInTheDocument();
  });

  it('calls onChange on click', async () => {
    const onChange = vi.fn();
    render(<Tabs tabs={tabs} activeKey="one" onChange={onChange} />);
    await userEvent.click(screen.getByRole('tab', { name: 'Tab Two' }));
    expect(onChange).toHaveBeenCalledWith('two');
  });

  it('bare mode renders tab bar only, no tabpanel', () => {
    render(<Tabs tabs={tabs} activeKey="one" onChange={vi.fn()} bare />);
    expect(screen.getByRole('tab', { name: 'Tab One' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Tab Two' })).toBeInTheDocument();
    // tabpanel should NOT be rendered
    expect(screen.queryByRole('tabpanel')).not.toBeInTheDocument();
    // content should NOT be rendered
    expect(screen.queryByText('Content 1')).not.toBeInTheDocument();
  });

  // GitHub #86/#87: the bar scrolls instead of wrapping or spilling out.
  it('keeps every tab single-line inside one scrollable tablist', () => {
    const many = ['Entries', 'Agent runs', 'Skills', 'History'].map((label) => ({ key: label, label }));
    render(<Tabs tabs={many} activeKey="Entries" onChange={vi.fn()} bare />);
    const list = screen.getByRole('tablist');
    expect(list.className).toContain('overflow-x-auto');
    expect(list.className).not.toContain('flex-wrap');
    for (const tab of screen.getAllByRole('tab')) {
      expect(tab.parentElement).toBe(list);
      expect(tab.className).toContain('shrink-0');
      expect(tab.className).toContain('whitespace-nowrap');
    }
  });

  function sizeList(list: HTMLElement, { clientWidth, scrollWidth }: { clientWidth: number; scrollWidth: number }) {
    Object.defineProperty(list, 'clientWidth', { configurable: true, value: clientWidth });
    Object.defineProperty(list, 'scrollWidth', { configurable: true, value: scrollWidth });
  }

  it('fades only the edges that have tabs past them', () => {
    render(<Tabs tabs={tabs} activeKey="one" onChange={vi.fn()} bare />);
    const list = screen.getByRole('tablist');
    expect(list).not.toHaveAttribute('data-overflow-right');

    sizeList(list, { clientWidth: 200, scrollWidth: 500 });
    fireEvent.scroll(list);
    expect(list).toHaveAttribute('data-overflow-right', 'true');
    expect(list).not.toHaveAttribute('data-overflow-left');

    list.scrollLeft = 300;
    fireEvent.scroll(list);
    expect(list).toHaveAttribute('data-overflow-left', 'true');
    expect(list).not.toHaveAttribute('data-overflow-right');
  });

  it('lets a plain mouse wheel scroll an overflowing bar, and the page at its ends', () => {
    render(<Tabs tabs={tabs} activeKey="one" onChange={vi.fn()} bare />);
    const list = screen.getByRole('tablist');
    sizeList(list, { clientWidth: 200, scrollWidth: 500 });

    // Not at the end yet: the wheel moves the bar, not the page.
    expect(fireEvent.wheel(list, { deltaY: 120 })).toBe(false);
    expect(list.scrollLeft).toBe(120);
    expect(list).toHaveAttribute('data-overflow-left', 'true');

    // Already fully scrolled right: the page gets the wheel.
    list.scrollLeft = 300;
    expect(fireEvent.wheel(list, { deltaY: 120 })).toBe(true);
    expect(list.scrollLeft).toBe(300);
    // Horizontal gestures are left to the browser.
    expect(fireEvent.wheel(list, { deltaX: -50, deltaY: 0 })).toBe(true);
  });

  it('leaves the wheel alone when every tab fits', () => {
    render(<Tabs tabs={tabs} activeKey="one" onChange={vi.fn()} bare />);
    const list = screen.getByRole('tablist');
    sizeList(list, { clientWidth: 500, scrollWidth: 500 });
    expect(fireEvent.wheel(list, { deltaY: 120 })).toBe(true);
    expect(list.scrollLeft).toBe(0);
  });

  it('scrolls the bar (not the page) to the active tab', () => {
    const pageScrolls = vi.mocked(HTMLElement.prototype.scrollIntoView).mock.calls.length;
    const { rerender } = render(<Tabs tabs={tabs} activeKey="one" onChange={vi.fn()} bare />);
    const list = screen.getByRole('tablist');
    sizeList(list, { clientWidth: 100, scrollWidth: 400 });
    const two = screen.getByRole('tab', { name: 'Tab Two' });
    Object.defineProperty(two, 'offsetLeft', { configurable: true, value: 300 });
    Object.defineProperty(two, 'offsetWidth', { configurable: true, value: 80 });
    rerender(<Tabs tabs={tabs} activeKey="two" onChange={vi.fn()} bare />);
    // 300 + 80 + 24px fade - 100 visible
    expect(list.scrollLeft).toBe(304);
    expect(vi.mocked(HTMLElement.prototype.scrollIntoView).mock.calls.length).toBe(pageScrolls);
  });

  describe('WAI-ARIA wiring', () => {
    const three = [
      { key: 'a', label: 'Alpha', content: <p>A</p> },
      { key: 'b', label: 'Beta', content: <p>B</p> },
      { key: 'c', label: 'Gamma', content: <p>C</p> },
    ];

    function Controlled() {
      const [key, setKey] = useState('a');
      return <Tabs tabs={three} activeKey={key} onChange={setKey} />;
    }

    it('only the selected tab is tabbable', () => {
      render(<Tabs tabs={three} activeKey="b" onChange={vi.fn()} />);
      expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveAttribute('tabindex', '-1');
      expect(screen.getByRole('tab', { name: 'Beta' })).toHaveAttribute('tabindex', '0');
      expect(screen.getByRole('tab', { name: 'Gamma' })).toHaveAttribute('tabindex', '-1');
    });

    it('ArrowRight/ArrowLeft move focus and select, wrapping at the ends', async () => {
      const user = userEvent.setup();
      render(<Controlled />);
      screen.getByRole('tab', { name: 'Alpha' }).focus();
      await user.keyboard('{ArrowRight}');
      expect(screen.getByRole('tab', { name: 'Beta' })).toHaveFocus();
      expect(screen.getByRole('tab', { name: 'Beta' })).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByText('B')).toBeInTheDocument();
      await user.keyboard('{ArrowRight}{ArrowRight}');
      expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveFocus();
      expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveAttribute('aria-selected', 'true');
      await user.keyboard('{ArrowLeft}');
      expect(screen.getByRole('tab', { name: 'Gamma' })).toHaveFocus();
      expect(screen.getByRole('tab', { name: 'Gamma' })).toHaveAttribute('aria-selected', 'true');
    });

    it('Home and End jump to the first and last tab', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      render(<Tabs tabs={three} activeKey="b" onChange={onChange} />);
      screen.getByRole('tab', { name: 'Beta' }).focus();
      await user.keyboard('{End}');
      expect(screen.getByRole('tab', { name: 'Gamma' })).toHaveFocus();
      expect(onChange).toHaveBeenLastCalledWith('c');
      await user.keyboard('{Home}');
      expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveFocus();
      expect(onChange).toHaveBeenLastCalledWith('a');
    });

    it('links each tab to its panel with aria-controls / aria-labelledby', () => {
      render(<Tabs tabs={three} activeKey="b" onChange={vi.fn()} aria-label="Letters" />);
      expect(screen.getByRole('tablist', { name: 'Letters' })).toBeInTheDocument();
      const tab = screen.getByRole('tab', { name: 'Beta' });
      const panel = screen.getByRole('tabpanel');
      expect(tab).toHaveAttribute('id');
      expect(tab.getAttribute('aria-controls')).toBe(panel.id);
      expect(panel.getAttribute('aria-labelledby')).toBe(tab.id);
      expect(panel).toHaveAttribute('tabindex', '0');
      expect(screen.getByRole('tab', { name: 'Alpha' })).not.toHaveAttribute('aria-controls');
    });

    it('TabPanel in bare mode resolves to the same ids via idPrefix', () => {
      render(
        <>
          <Tabs tabs={three} activeKey="c" onChange={vi.fn()} bare idPrefix="demo" />
          <TabPanel idPrefix="demo" tabKey="c">C body</TabPanel>
        </>,
      );
      const tab = screen.getByRole('tab', { name: 'Gamma' });
      const panel = screen.getByRole('tabpanel');
      expect(tab.getAttribute('aria-controls')).toBe(panel.id);
      expect(panel.getAttribute('aria-labelledby')).toBe(tab.id);
    });

    it('two tablists on one page do not share ids', () => {
      render(
        <>
          <Tabs tabs={three} activeKey="a" onChange={vi.fn()} aria-label="First" />
          <Tabs tabs={three} activeKey="a" onChange={vi.fn()} aria-label="Second" />
        </>,
      );
      const ids = screen.getAllByRole('tab').map((t) => t.id);
      expect(new Set(ids).size).toBe(ids.length);
      const panelIds = screen.getAllByRole('tabpanel').map((p) => p.id);
      expect(new Set(panelIds).size).toBe(panelIds.length);
    });
  });
});

// ── Table ──

describe('Table', () => {
  type Row = { name: string; count: number };
  const cols: Array<{ key: string; header: string; render: (r: Row) => React.ReactNode }> = [
    { key: 'name', header: 'Name', render: (r) => r.name },
    { key: 'count', header: 'Count', render: (r) => r.count },
  ];
  const data: Row[] = [
    { name: 'A', count: 1 },
    { name: 'B', count: 2 },
  ];

  it('renders headers and data rows', () => {
    render(<Table columns={cols} data={data} rowKey={(r) => r.name} />);
    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(screen.getByText('Count')).toBeInTheDocument();
    expect(screen.getByText('A')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('calls onRowClick when row clicked', async () => {
    const fn = vi.fn();
    render(<Table columns={cols} data={data} rowKey={(r) => r.name} onRowClick={fn} />);
    await userEvent.click(screen.getByText('A'));
    expect(fn).toHaveBeenCalledWith(data[0]);
  });

  it('shows empty state when no data', () => {
    render(<Table columns={cols} data={[]} rowKey={(r: Row) => r.name} />);
    expect(screen.getByText('No data')).toBeInTheDocument();
  });
});

// ── Dropdown ──

describe('Dropdown', () => {
  const items = [
    { key: 'edit', label: 'Edit' },
    { key: 'delete', label: 'Delete', danger: true },
    { key: 'disabled', label: 'Nope', disabled: true },
  ];

  it('opens on trigger click', async () => {
    render(
      <Dropdown trigger={<button>Menu</button>} items={items} onSelect={vi.fn()} menuLabel="Actions" />,
    );
    await userEvent.click(screen.getByText('Menu'));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toBeInTheDocument();
  });

  it('calls onSelect when item clicked', async () => {
    const fn = vi.fn();
    render(
      <Dropdown trigger={<button>Menu</button>} items={items} onSelect={fn} menuLabel="Actions" />,
    );
    await userEvent.click(screen.getByText('Menu'));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Edit' }));
    expect(fn).toHaveBeenCalledWith('edit');
  });

  it('renders separator items as dividers, not buttons', async () => {
    const itemsWithSep = [
      { key: 'edit', label: 'Edit' },
      { key: 'sep', label: '', separator: true },
      { key: 'delete', label: 'Delete', danger: true },
    ];
    render(
      <Dropdown trigger={<button>Menu</button>} items={itemsWithSep} onSelect={vi.fn()} menuLabel="Actions" />,
    );
    await userEvent.click(screen.getByText('Menu'));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    // Separator should NOT be a menuitem
    expect(screen.queryByRole('menuitem', { name: '' })).not.toBeInTheDocument();
    // Other items still present
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Delete' })).toBeInTheDocument();
  });

  it('closes on Escape (Dropdown)', async () => {
    render(
      <Dropdown trigger={<button>Menu</button>} items={items} onSelect={vi.fn()} menuLabel="Actions" />,
    );
    await userEvent.click(screen.getByText('Menu'));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });
  });
});

// ── Tooltip ──

describe('Tooltip', () => {
  it('renders trigger children', () => {
    render(<Tooltip content="Help text"><button>Hover me</button></Tooltip>);
    expect(screen.getByText('Hover me')).toBeInTheDocument();
  });

  it('shows tooltip on hover (after 400ms)', async () => {
    render(<Tooltip content="Help text"><button>Hover me</button></Tooltip>);
    fireEvent.mouseEnter(screen.getByText('Hover me'));
    // Tooltip appears after 400ms delay — wait for it
    await waitFor(
      () => {
        expect(screen.getByRole('tooltip')).toHaveTextContent('Help text');
      },
      { timeout: 1000 },
    );
  });
});

// ── Kbd ──

describe('Kbd', () => {
  it('renders a kbd element', () => {
    render(<Kbd>⌘</Kbd>);
    expect(screen.getByText('⌘').tagName).toBe('KBD');
  });

  it('has mono-chip styling classes', () => {
    render(<Kbd>K</Kbd>);
    const el = screen.getByText('K');
    expect(el).toHaveClass('font-mono');
    expect(el).toHaveClass('bg-bg-sunken');
  });
});

describe('KbdShortcut', () => {
  it('renders multiple Kbd elements', () => {
    render(<KbdShortcut keys={['⌘', 'K']} />);
    expect(screen.getByText('⌘')).toBeInTheDocument();
    expect(screen.getByText('K')).toBeInTheDocument();
  });
});

// ── PageHeader / PageContainer ──

describe('PageHeader', () => {
  it('renders the title as the single h1 with optional slots', () => {
    render(
      <PageHeader
        title="Billing"
        description="Manage your plan"
        actions={<button>Act</button>}
        breadcrumb={<a href="/">Back</a>}
      />,
    );
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { level: 1, name: 'Billing' })).toHaveClass('text-3xl', 'font-bold');
    expect(screen.getByText('Manage your plan')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Act' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Back' })).toBeInTheDocument();
  });

  it('omits optional slots when not given', () => {
    render(<PageHeader title="Only" />);
    expect(screen.getByRole('heading', { level: 1, name: 'Only' })).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('PageContainer', () => {
  it('applies width presets and does not add a second main landmark', () => {
    const { rerender } = render(<PageContainer data-testid="pc">x</PageContainer>);
    expect(screen.getByTestId('pc')).toHaveClass('max-w-5xl', 'mx-auto', 'px-4', 'py-6');
    expect(screen.queryByRole('main')).not.toBeInTheDocument();

    rerender(<PageContainer width="narrow" data-testid="pc">x</PageContainer>);
    expect(screen.getByTestId('pc')).toHaveClass('max-w-3xl');

    rerender(<PageContainer width="wide" data-testid="pc">x</PageContainer>);
    expect(screen.getByTestId('pc')).toHaveClass('max-w-7xl');

    rerender(<PageContainer width="full" data-testid="pc">x</PageContainer>);
    expect(screen.getByTestId('pc').className).not.toMatch(/max-w-/);
  });
});

describe('Select listbox semantics', () => {
  it('hands focus to the listbox and exposes the active option while open', async () => {
    render(
      <Select
        value=""
        onValueChange={vi.fn()}
        options={[
          { value: 'a', label: 'Alpha' },
          { value: 'b', label: 'Beta' },
        ]}
        aria-label="Pick"
      />,
    );
    const trigger = screen.getByRole('button', { name: 'Pick' });
    expect(trigger).not.toHaveAttribute('role', 'combobox');
    await userEvent.click(trigger);
    const list = screen.getByRole('listbox');
    expect(list).toHaveFocus();
    await userEvent.keyboard('{ArrowDown}');
    expect(list.getAttribute('aria-activedescendant')).toBe(screen.getByRole('option', { name: 'Alpha' }).id);
    await userEvent.keyboard('{Escape}');
    expect(trigger).toHaveFocus();
  });
});
