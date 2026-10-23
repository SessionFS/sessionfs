import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { MonoText } from '../';

describe('MonoText', () => {
  it('keeps the full value in the title and text content', () => {
    const value = 'github.com/acme-corporation/very-long-repository-name';
    const { container } = render(<MonoText value={value} />);
    expect(container.querySelector(`[title="${value}"]`)).not.toBeNull();
    expect(container).toHaveTextContent(value);
  });

  it('truncates the start and keeps the tail visible', () => {
    const { container } = render(<MonoText value="github.com/acme/backend-service" tailChars={7} />);
    const truncated = container.querySelector('.truncate');
    expect(truncated).toHaveTextContent('github.com/acme/backend-');
    expect(truncated?.nextElementSibling).toHaveTextContent('service');
  });

  it('does not overflow its container', () => {
    const { container } = render(<MonoText value={'x'.repeat(200)} />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain('min-w-0');
    expect(root.className).toContain('max-w-full');
  });

  it('renders no copy button by default', () => {
    render(<MonoText value="abc" />);
    expect(screen.queryByRole('button', { name: 'Copy' })).toBeNull();
  });

  it('copies the full value and shows an inline Copied state', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<MonoText value="ses_abcdef0123456789" copyable />);
    await user.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith('ses_abcdef0123456789');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Copied'));
  });
});
