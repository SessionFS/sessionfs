import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type JiraTemplate } from '../api/client';
import { axe } from '../test/axe';
import { TemplateEditorDialog, TemplatesSection } from './JiraTemplates';

/**
 * Org Settings → Jira → Story templates. Covers the list (built-ins read-only,
 * archive/unarchive), the section editor (per-mode source fields, keyboard
 * reordering with stable focus, optimistic-concurrency conflict), and the
 * deterministic draft preview against a real ticket — plus axe.
 */

const mockAuth = vi.fn();
const client = {
  listJiraTemplates: vi.fn(),
  createJiraTemplate: vi.fn(),
  updateJiraTemplate: vi.fn(),
  deleteJiraTemplate: vi.fn(),
  unarchiveJiraTemplate: vi.fn(),
  listJiraTemplateVersions: vi.fn(),
  previewJiraTemplate: vi.fn(),
  listJiraMappings: vi.fn(),
  listProjects: vi.fn(),
  listTickets: vi.fn(),
};
const addToast = vi.fn();

vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function renderNode(node: ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

function template(over: Partial<JiraTemplate> = {}): JiraTemplate {
  return {
    slug: 'design-lite',
    name: 'Design Lite',
    description: null,
    sections: [
      { key: 'summary', heading: 'Summary', mode: 'rewrite', source_fields: ['title'], guidance: null },
      {
        key: 'acceptance_criteria',
        heading: 'Acceptance Criteria',
        mode: 'verbatim',
        source_fields: ['acceptance_criteria'],
        guidance: null,
      },
    ],
    version: 4,
    builtin: false,
    archived_at: null,
    created_at: null,
    updated_at: null,
    created_by_user_id: null,
    updated_by_user_id: null,
    ...over,
  };
}

const BUILTIN = template({ slug: 'agile', name: 'Agile Standard', version: null, builtin: true });

beforeEach(() => {
  for (const fn of Object.values(client)) fn.mockReset();
  addToast.mockReset();
  mockAuth.mockReturnValue({ auth: { apiKey: 'k', baseUrl: 'http://test.api', client } });
  client.listJiraTemplates.mockResolvedValue([BUILTIN, template()]);
  client.listJiraMappings.mockResolvedValue([
    { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Story', template: null, done_status_name: null },
  ]);
  client.listProjects.mockResolvedValue([
    { id: 'proj_a', name: 'Alpha', git_remote_normalized: 'x', context_document: '', owner_id: 'u', org_id: 'org_x', created_at: '', updated_at: '' },
  ]);
  client.listTickets.mockResolvedValue([{ id: 'tk_1', title: 'Fix login' }]);
});

function editor(initial?: JiraTemplate, onSaved = vi.fn()) {
  return renderNode(
    <TemplateEditorDialog
      orgId="org_x"
      existingSlugs={new Set(['agile'])}
      initial={initial}
      onClose={() => {}}
      onSaved={onSaved}
    />,
  );
}

describe('TemplatesSection', () => {
  it('lists built-ins read-only and custom templates with actions', async () => {
    const { container } = renderNode(<TemplatesSection orgId="org_x" />);
    expect(await screen.findByText('Design Lite')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit Design Lite' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Archive Design Lite' })).toBeInTheDocument();
    // Built-ins fold into one read-only line until expanded.
    const builtins = screen.getByRole('button', { name: /built-in template/ });
    expect(builtins).toHaveAttribute('aria-expanded', 'false');
    expect(builtins).toHaveTextContent('Read-only');
    expect(screen.queryByText('Agile Standard')).not.toBeInTheDocument();
    await userEvent.setup().click(builtins);
    expect(screen.getByText('Agile Standard')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit Agile Standard' })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('unarchives an archived template', async () => {
    client.listJiraTemplates.mockResolvedValue([
      BUILTIN,
      template({ archived_at: '2026-10-01T00:00:00Z' }),
    ]);
    client.unarchiveJiraTemplate.mockResolvedValue(template());
    renderNode(<TemplatesSection orgId="org_x" />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Unarchive Design Lite' }));
    await waitFor(() => expect(client.unarchiveJiraTemplate).toHaveBeenCalledWith('org_x', 'design-lite'));
    expect(addToast).toHaveBeenCalledWith('success', 'Design Lite unarchived');
    expect(screen.queryByRole('button', { name: 'Edit Design Lite' })).not.toBeInTheDocument();
  });
});

describe('TemplateEditorDialog', () => {
  it('offers only the source fields a verbatim section can render', async () => {
    const { container } = editor();
    const user = userEvent.setup();
    const section = screen.getByRole('group', { name: 'Section 1' });
    // Rewrite: the whole vocabulary.
    expect(within(section).getByRole('checkbox', { name: 'Title' })).toBeInTheDocument();

    await user.click(within(section).getByRole('button', { name: /Mode/ }));
    await user.click(screen.getByRole('option', { name: 'Verbatim' }));
    expect(within(section).getAllByRole('checkbox')).toHaveLength(3);
    for (const name of ['Acceptance criteria', 'Completion notes', 'Linked sessions']) {
      expect(within(section).getByRole('checkbox', { name })).toBeInTheDocument();
    }
    expect(within(section).queryByRole('checkbox', { name: 'Title' })).not.toBeInTheDocument();
    // Title (unrenderable verbatim) was dropped; acceptance criteria picked instead.
    expect(within(section).getByRole('checkbox', { name: 'Acceptance criteria' })).toBeChecked();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('reorders sections by keyboard and focus follows the moved section', async () => {
    editor(template());
    const user = userEvent.setup();
    const down = screen.getByRole('button', { name: 'Move Summary down' });
    down.focus();
    await user.keyboard('{Enter}');

    const groups = screen.getAllByRole('group', { name: /Section \d/ });
    expect(within(groups[0]).getByDisplayValue('Acceptance Criteria')).toBeInTheDocument();
    expect(within(groups[1]).getByDisplayValue('Summary')).toBeInTheDocument();
    // At the bottom edge "down" is disabled, so focus lands on "up" of the
    // same (moved) section.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Move Summary up' })).toHaveFocus(),
    );
    // The moved section kept its own state (stable identity, not index keys).
    expect(within(groups[1]).getByRole('checkbox', { name: 'Title' })).toBeChecked();
  });

  it('sends expected_version on save and explains a version conflict', async () => {
    client.updateJiraTemplate.mockRejectedValue(
      new ApiError(
        409,
        JSON.stringify({
          error: { code: '409', message: 'changed', details: { error: 'template_version_conflict' } },
        }),
      ),
    );
    const onSaved = vi.fn();
    editor(template(), onSaved);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(client.updateJiraTemplate).toHaveBeenCalled());
    expect(client.updateJiraTemplate.mock.calls[0][1]).toBe('design-lite');
    expect(client.updateJiraTemplate.mock.calls[0][2]).toMatchObject({ expected_version: 4 });
    expect(await screen.findByText(/Someone else saved this template/)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('previews the unsaved draft against a ticket from a mapped project', async () => {
    client.previewJiraTemplate.mockResolvedValue({
      ticket_id: 'tk_1',
      project_id: 'proj_a',
      template: { slug: 'design-lite', name: 'Design Lite', version: null },
      summary: 'Fix login',
      sections: [
        { key: 'summary', heading: 'Summary', mode: 'rewrite', paragraphs: [{ text: 'Fix login', citations: ['title'] }] },
      ],
    });
    const { container } = editor(template());
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /Mapped project/ }));
    await user.click(await screen.findByRole('option', { name: 'Alpha' }));
    await user.click(screen.getByRole('button', { name: /Ticket/ }));
    await user.click(await screen.findByRole('option', { name: 'Fix login (tk_1)' }));
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    await waitFor(() => expect(client.previewJiraTemplate).toHaveBeenCalled());
    const [orgId, slug, body] = client.previewJiraTemplate.mock.calls[0];
    expect(orgId).toBe('org_x');
    expect(slug).toBe('design-lite');
    expect(body).toMatchObject({ project_id: 'proj_a', ticket_id: 'tk_1', name: 'Design Lite' });
    expect(body.sections.map((s: { key: string }) => s.key)).toEqual(['summary', 'acceptance_criteria']);

    const region = screen.getByRole('region', { name: 'Template preview' });
    expect(await within(region).findByText('Fix login', { selector: 'p.whitespace-pre-wrap' })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});
