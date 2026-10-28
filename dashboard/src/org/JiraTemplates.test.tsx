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

    await user.click(within(section).getByRole('button', { name: /^Mode$/ }));
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
    await user.click(await screen.findByRole('option', { name: /^Alpha/ }));
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

  it('copies sections from a built-in template', async () => {
    client.listJiraTemplates.mockResolvedValue([
      template({
        slug: 'agile',
        name: 'Agile Standard',
        version: null,
        builtin: true,
        sections: [
          { key: 'story', heading: 'User Story', mode: 'rewrite', source_fields: ['title', 'description'], guidance: null },
          { key: 'ac', heading: 'Acceptance Criteria', mode: 'verbatim', source_fields: ['acceptance_criteria'], guidance: null },
          { key: 'notes', heading: 'Notes', mode: 'verbatim', source_fields: ['completion_notes'], guidance: null },
        ],
      }),
      template(),
    ]);
    editor();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Copy sections from a template' }));
    expect(screen.getByRole('option', { name: 'Design Lite (2 sections)' })).toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: 'Built-in · Agile Standard (3 sections)' }));

    const groups = screen.getAllByRole('group', { name: /Section \d/ });
    expect(groups).toHaveLength(3);
    expect(within(groups[0]).getByDisplayValue('User Story')).toBeInTheDocument();
    expect(within(groups[0]).getByRole('checkbox', { name: 'Description' })).toBeChecked();
    expect(screen.getByRole('status')).toHaveTextContent('Sections copied from Agile Standard');
  });

  it('explains mode and each source field', async () => {
    const { container } = editor();
    const user = userEvent.setup();
    const section = screen.getByRole('group', { name: 'Section 1' });
    const modeInfo = within(section).getByRole('button', { name: 'About Mode' });
    expect(modeInfo).toHaveAccessibleDescription(/every paragraph it writes must cite the ticket field/);
    expect(within(section).getByRole('button', { name: 'About source fields' })).toHaveAccessibleDescription(
      /in the order you ticked them/,
    );
    expect(within(section).getByRole('button', { name: 'About Linked sessions' })).toBeInTheDocument();
    // Tap/click shows the bubble too (no hover on touch screens).
    await user.click(modeInfo);
    expect(await screen.findByRole('tooltip')).toHaveTextContent(/Verbatim: copied from the ticket exactly/);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('searches mapped projects in the preview picker', async () => {
    client.listJiraMappings.mockResolvedValue([
      { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Story', template: null, done_status_name: null },
      { project_id: 'proj_b', jira_project_key: 'BETA', issue_type: 'Story', template: null, done_status_name: null },
    ]);
    client.listProjects.mockResolvedValue([
      { id: 'proj_a', name: 'Alpha', git_remote_normalized: 'x', context_document: '', owner_id: 'u', org_id: 'org_x', created_at: '', updated_at: '' },
      { id: 'proj_b', name: 'Beta', git_remote_normalized: 'y', context_document: '', owner_id: 'u', org_id: 'org_x', created_at: '', updated_at: '' },
    ]);
    editor(template());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Mapped project/ }));
    await screen.findByRole('option', { name: /^Beta/ });
    await user.type(screen.getByRole('combobox'), 'bet');
    expect(screen.queryByRole('option', { name: /^Alpha/ })).toBeNull();
    await user.click(screen.getByRole('option', { name: /^Beta/ }));
    expect(screen.getByRole('button', { name: /Mapped project/ })).toHaveTextContent('Beta');
  });

  it('asks before copying over edited sections; Escape closes only the confirm', async () => {
    editor(template());
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Copy sections from a template' }));
    await user.click(screen.getByRole('option', { name: /Agile Standard/ }));

    const confirm = screen.getByRole('dialog', { name: 'Replace your sections?' });
    expect(confirm).toHaveAttribute('aria-modal', 'true');
    // Only the topmost dialog is modal; the editor under it is inert and
    // hidden from assistive tech until the confirm closes.
    expect(screen.queryByRole('dialog', { name: 'Edit template' })).toBeNull();
    const editorPanel = screen.getByRole('dialog', { name: 'Edit template', hidden: true });
    expect(editorPanel).not.toHaveAttribute('aria-modal');
    const editorRoot = editorPanel.parentElement!;
    expect(editorRoot).toHaveAttribute('aria-hidden', 'true');
    expect(editorRoot.hasAttribute('inert')).toBe(true);
    expect(screen.getAllByRole('dialog')).toHaveLength(1);

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Replace your sections?' })).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Edit template' })).toHaveAttribute('aria-modal', 'true');
    expect(editorRoot.hasAttribute('inert')).toBe(false);
    expect(editorRoot).not.toHaveAttribute('aria-hidden');
    // Focus goes back to the trigger the confirm came from.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Copy sections from a template' })).toHaveFocus(),
    );
    expect(screen.getAllByRole('group', { name: /Section \d/ })).toHaveLength(2);
    expect(screen.getByDisplayValue('Summary')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Copy sections from a template' }));
    await user.click(screen.getByRole('option', { name: /Agile Standard/ }));
    await user.click(screen.getByRole('button', { name: 'Replace sections' }));
    expect(screen.getByRole('status')).toHaveTextContent('Sections copied from Agile Standard');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Copy sections from a template' })).toHaveFocus(),
    );
  });

  it('closes an info tooltip on Escape without closing the editor', async () => {
    const onClose = vi.fn();
    renderNode(
      <TemplateEditorDialog orgId="org_x" existingSlugs={new Set()} onClose={onClose} onSaved={vi.fn()} />,
    );
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: 'About Mode' })[0]);
    expect(await screen.findByRole('tooltip')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('tooltip')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
