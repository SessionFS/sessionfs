import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Markdown } from './Markdown';

describe('Markdown', () => {
  it('renders lists as lists', () => {
    const { container } = render(<Markdown>{'- one\n- two\n\n1. first'}</Markdown>);
    expect(container.querySelectorAll('ul > li')).toHaveLength(2);
    expect(container.querySelectorAll('ol > li')).toHaveLength(1);
  });

  it('keeps single line breaks', () => {
    const { container } = render(<Markdown>{'Session ID: abc\nKeep this block at the top'}</Markdown>);
    expect(container.querySelector('p br')).not.toBeNull();
  });

  it('renders GitHub tables inside a scroll container', () => {
    const { container } = render(<Markdown>{'| a | b |\n|---|---|\n| 1 | 2 |'}</Markdown>);
    const table = container.querySelector('table');
    expect(table).not.toBeNull();
    expect(table!.parentElement!.className).toContain('overflow-x-auto');
    expect(container.querySelectorAll('td')).toHaveLength(2);
  });

  it('never renders raw HTML', () => {
    const { container } = render(<Markdown>{'hi <img src=x onerror="alert(1)"> <b>bold</b>'}</Markdown>);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });

  it('drops unsafe link targets and opens external ones in a new tab', () => {
    render(<Markdown>{'[bad](javascript:alert(1)) [good](https://example.com) [here](#ctx-a)'}</Markdown>);
    expect(screen.queryByRole('link', { name: 'bad' })).toBeNull();
    expect(screen.getByText('bad')).toBeInTheDocument();
    const good = screen.getByRole('link', { name: 'good' });
    expect(good).toHaveAttribute('target', '_blank');
    expect(good).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByRole('link', { name: 'here' })).not.toHaveAttribute('target');
  });

  it('keeps root-relative links only when they stay on this origin', () => {
    render(<Markdown>{'[home](/projects/p1) [slash](/\\evil.example) [proto](//evil.example)'}</Markdown>);
    expect(screen.getByRole('link', { name: 'home' })).toHaveAttribute('href', '/projects/p1');
    // The markdown parser percent-encodes the backslash; either way the link
    // must not leave this origin.
    const slash = screen.queryByRole('link', { name: 'slash' });
    if (slash) {
      expect(new URL(slash.getAttribute('href')!, window.location.origin).origin).toBe(window.location.origin);
    }
    expect(screen.queryByRole('link', { name: 'proto' })).toBeNull();
  });

  it('gives headings unique ids only when asked', () => {
    const md = '## Known Issues\n\n## Known Issues\n\n### `k8s` migration';
    const { container, rerender } = render(<Markdown>{md}</Markdown>);
    expect(container.querySelector('h2[id]')).toBeNull();
    rerender(<Markdown headingIdPrefix="ctx">{md}</Markdown>);
    const ids = Array.from(container.querySelectorAll('h2, h3')).map((h) => h.id);
    expect(ids).toEqual(['ctx-known-issues', 'ctx-known-issues-2', 'ctx-k8s-migration']);
  });
});
