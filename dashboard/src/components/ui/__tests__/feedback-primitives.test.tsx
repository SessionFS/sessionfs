import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Alert, IconButton, Checkbox, Switch } from '../';

describe('Alert', () => {
  it('uses role=alert for danger and role=status otherwise', () => {
    const { rerender } = render(<Alert variant="danger">Bad</Alert>);
    expect(screen.getByRole('alert')).toHaveTextContent('Bad');
    for (const v of ['info', 'success', 'warning'] as const) {
      rerender(<Alert variant={v}>Fine</Alert>);
      expect(screen.getByRole('status')).toHaveTextContent('Fine');
      expect(screen.queryByRole('alert')).toBeNull();
    }
  });

  it('renders title, body and action slot', () => {
    render(
      <Alert title="Heads up" action={<button>Undo</button>}>
        Details here
      </Alert>,
    );
    expect(screen.getByText('Heads up')).toBeInTheDocument();
    expect(screen.getByText('Details here')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument();
  });

  it('tints from the semantic token via color-mix', () => {
    render(<Alert variant="warning">w</Alert>);
    expect(screen.getByRole('status').getAttribute('style')).toContain('--warning');
  });
});

describe('IconButton', () => {
  it('exposes label as accessible name and title, defaults to type=button', () => {
    render(<IconButton label="Close"><svg /></IconButton>);
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn).toHaveAttribute('title', 'Close');
    expect(btn).toHaveAttribute('type', 'button');
  });

  it('fires onClick and respects variant/size', async () => {
    const onClick = vi.fn();
    render(<IconButton label="Go" variant="danger" size="sm" onClick={onClick}><svg /></IconButton>);
    const btn = screen.getByRole('button', { name: 'Go' });
    expect(btn).toHaveClass('bg-[var(--danger)]', 'p-1');
    await userEvent.click(btn);
    expect(onClick).toHaveBeenCalledOnce();
  });
});

describe('Checkbox', () => {
  it('associates label and toggles', async () => {
    const onChange = vi.fn();
    render(<Checkbox label="Remember me" onChange={onChange} />);
    await userEvent.click(screen.getByLabelText('Remember me'));
    expect(onChange).toHaveBeenCalledOnce();
  });

  it('honours disabled', () => {
    render(<Checkbox label="Nope" disabled />);
    expect(screen.getByLabelText('Nope')).toBeDisabled();
  });
});

describe('Switch', () => {
  function Harness() {
    const [on, setOn] = useState(false);
    return <Switch checked={on} onChange={setOn} label="Notifications" />;
  }

  it('has role=switch with aria-checked and toggles by click and keyboard', async () => {
    render(<Harness />);
    const sw = screen.getByRole('switch', { name: 'Notifications' });
    expect(sw).toHaveAttribute('aria-checked', 'false');
    await userEvent.click(sw);
    expect(sw).toHaveAttribute('aria-checked', 'true');
    sw.focus();
    await userEvent.keyboard(' ');
    expect(sw).toHaveAttribute('aria-checked', 'false');
    await userEvent.keyboard('{Enter}');
    expect(sw).toHaveAttribute('aria-checked', 'true');
  });
});
