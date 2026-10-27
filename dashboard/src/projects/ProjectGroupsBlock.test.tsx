import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import ProjectGroupsBlock from './ProjectGroupsBlock';
import type { ProjectGroupSummary } from '../api/client';

const group = (extra: Partial<ProjectGroupSummary> = {}): ProjectGroupSummary => ({
  id: 'pg_1',
  org_id: 'org_1',
  slug: 'payments',
  name: 'Payments',
  description: null,
  member_count: 1,
  project_count: 1,
  my_role: 'editor',
  ...extra,
});

const renderBlock = (groups: ProjectGroupSummary[] | null | undefined) =>
  render(
    <MemoryRouter>
      <ProjectGroupsBlock groups={groups} />
    </MemoryRouter>,
  );

describe('ProjectGroupsBlock', () => {
  it('lists the groups with links and the caller’s role', () => {
    renderBlock([group(), group({ id: 'pg_2', name: 'Platform', my_role: 'admin' })]);
    expect(screen.getByRole('heading', { name: 'Groups' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Payments' })).toHaveAttribute(
      'href',
      '/settings/organization/groups/pg_1?org=org_1',
    );
    expect(screen.getByText('You: Editor')).toBeInTheDocument();
    expect(screen.getByText('You: Admin')).toBeInTheDocument();
  });

  it('does not link groups the caller has no standing on', () => {
    renderBlock([group({ my_role: null })]);
    expect(screen.getByText('Payments')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it.each([[[]], [null], [undefined]])('renders nothing for %p', (groups) => {
    const { container } = renderBlock(groups as ProjectGroupSummary[] | null | undefined);
    expect(container).toBeEmptyDOMElement();
  });
});
