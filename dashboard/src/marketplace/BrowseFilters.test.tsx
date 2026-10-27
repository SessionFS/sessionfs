import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FiltersMenu } from './BrowseFilters';

const NARROW = '(max-width: 639px)';
let original: typeof window.matchMedia;

function mountRoot() {
  const root = document.createElement('div');
  root.id = 'root';
  document.body.appendChild(root);
  return root;
}

beforeEach(() => {
  original = window.matchMedia;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === NARROW,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
});

afterEach(() => {
  window.matchMedia = original;
  document.getElementById('root')?.remove();
  document.body.style.overflow = '';
});

describe('FiltersMenu phone sheet', () => {
  it('makes the app inert and locks scrolling while open, and restores both on close', async () => {
    const user = userEvent.setup();
    const root = mountRoot();
    render(<FiltersMenu filters={{ sort: 'relevance' }} onChange={vi.fn()} />, { container: root });
    expect(root).not.toHaveAttribute('inert');

    await user.click(screen.getByRole('button', { name: 'Filters' }));
    const sheet = screen.getByRole('dialog', { name: 'Filters' });
    // The sheet lives outside the inert app root, so it stays usable.
    expect(root.contains(sheet)).toBe(false);
    expect(sheet).toHaveAttribute('aria-modal', 'true');
    expect(root).toHaveAttribute('inert');
    expect(document.body.style.overflow).toBe('hidden');

    await user.click(screen.getByRole('button', { name: 'Done' }));
    expect(root).not.toHaveAttribute('inert');
    expect(document.body.style.overflow).toBe('');
    expect(screen.getByRole('button', { name: 'Filters' })).toHaveFocus();
  });

  it('removes inert when unmounted while open', async () => {
    const user = userEvent.setup();
    const root = mountRoot();
    const view = render(<FiltersMenu filters={{ sort: 'relevance' }} onChange={vi.fn()} />, { container: root });
    await user.click(screen.getByRole('button', { name: 'Filters' }));
    expect(root).toHaveAttribute('inert');
    view.unmount();
    expect(root).not.toHaveAttribute('inert');
    expect(document.body.style.overflow).toBe('');
  });

  it('stays a non-modal popover on wide screens', async () => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })) as unknown as typeof window.matchMedia;
    const user = userEvent.setup();
    const root = mountRoot();
    render(<FiltersMenu filters={{ sort: 'relevance' }} onChange={vi.fn()} />, { container: root });
    await user.click(screen.getByRole('button', { name: 'Filters' }));
    expect(root).not.toHaveAttribute('inert');
    expect(screen.getByRole('dialog', { name: 'Filters' })).not.toHaveAttribute('aria-modal');
  });
});
