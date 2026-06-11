import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Button, Card, Input, Textarea, Select, Dialog, DialogHeader, DialogFooter, Tabs, Table, Dropdown, Tooltip, Kbd, KbdShortcut } from '../';

// ── Button ──

describe('Button', () => {
  it('renders children', () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('applies variant classes', () => {
    const { rerender } = render(<Button variant="primary">A</Button>);
    expect(screen.getByRole('button')).toHaveClass('bg-[var(--brand)]');

    rerender(<Button variant="secondary">B</Button>);
    expect(screen.getByRole('button')).toHaveClass('border');

    rerender(<Button variant="ghost">C</Button>);
    expect(screen.getByRole('button')).toHaveClass('bg-transparent');

    rerender(<Button variant="danger">D</Button>);
    expect(screen.getByRole('button')).toHaveClass('bg-[var(--danger)]');
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
});

describe('Select', () => {
  const opts = [
    { value: 'a', label: 'Alpha' },
    { value: 'b', label: 'Beta' },
  ];

  it('renders options', () => {
    render(<Select options={opts} />);
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    expect(screen.getByText('Alpha')).toBeInTheDocument();
    expect(screen.getByText('Beta')).toBeInTheDocument();
  });

  it('shows error state', () => {
    render(<Select options={opts} error="Pick one" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Pick one');
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
    expect(el).toHaveClass('bg-[var(--bg-sunken)]');
  });
});

describe('KbdShortcut', () => {
  it('renders multiple Kbd elements', () => {
    render(<KbdShortcut keys={['⌘', 'K']} />);
    expect(screen.getByText('⌘')).toBeInTheDocument();
    expect(screen.getByText('K')).toBeInTheDocument();
  });
});
