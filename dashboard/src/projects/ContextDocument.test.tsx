import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import ContextDocument from './ContextDocument';

const LONG = [
  '## Kubernetes migration',
  'Dev copy is live.',
  '# Project Context',
  '## Known Issues',
  '- one',
  '## Key Decisions',
  '- two',
  '## Recent Changes',
  '### 2026-10-23',
  '- three',
].join('\n\n');

describe('ContextDocument', () => {
  it('lists the sections, leaving out a lone document title', () => {
    render(<ContextDocument markdown={LONG} />);
    // One list for wide screens, one inside the collapsible for narrow ones.
    const navs = screen.getAllByRole('navigation', { name: 'Context sections' });
    expect(navs).toHaveLength(2);
    const names = within(navs[0]).getAllByRole('link').map((a) => a.textContent);
    expect(names).toEqual(['Kubernetes migration', 'Known Issues', 'Key Decisions', 'Recent Changes', '2026-10-23']);
  });

  it('links each entry to the heading the renderer gave an id', () => {
    const { container } = render(<ContextDocument markdown={LONG} />);
    const nav = screen.getAllByRole('navigation', { name: 'Context sections' })[0];
    for (const link of within(nav).getAllByRole('link')) {
      const id = link.getAttribute('href')!.slice(1);
      expect(container.querySelector(`#${id}`)).not.toBeNull();
    }
  });

  it('shows no table of contents for a short document', () => {
    render(<ContextDocument markdown={'# Docs site\n\nPublic documentation.\n\n## Setup'} />);
    expect(screen.queryByRole('navigation', { name: 'Context sections' })).toBeNull();
    expect(screen.getByText('Public documentation.')).toBeInTheDocument();
  });
});
