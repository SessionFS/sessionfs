import type { ReactNode } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type JiraPreview, type Ticket } from '../../api/client';
import { axe } from '../../test/axe';
import { JiraTicketSection, PromoteToJiraDialog } from './JiraPromote';

/**
 * Ticket-detail Jira surface. The badge reflects the ticket's embedded link
 * (and live status via GET /jira); the dialog previews an editable draft and
 * publishes the edited content back. Error codes arrive in
 * `ApiError.details.error`, so this suite drives every code through the
 * preview error state (the same `friendlyError` used for publish/unlink).
 */

const mockUseJiraLink = vi.fn();
const mockUseJiraPreview = vi.fn();
const mockUsePublishJira = vi.fn();
const mockUseUnlinkJira = vi.fn();
const addToast = vi.fn();

vi.mock('../../hooks/useJiraTickets', () => ({
  useJiraLink: (...args: unknown[]) => mockUseJiraLink(...args),
  useJiraPreview: (...args: unknown[]) => mockUseJiraPreview(...args),
  usePublishJira: (...args: unknown[]) => mockUsePublishJira(...args),
  useUnlinkJira: (...args: unknown[]) => mockUseUnlinkJira(...args),
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

function renderNode(node: ReactNode) {
  const qc = makeQueryClient();
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

/** The re-enveloped error shape the Jira routes produce on failure. */
function apiError(code: string): ApiError {
  return new ApiError(
    409,
    JSON.stringify({ error: { code: '409', message: 'Conflict', details: { error: code } } }),
  );
}

const baseTicket: Ticket = {
  id: 't_1',
  project_id: 'proj_1',
  title: 'Fix login',
  description: 'description',
  priority: 'high',
  assigned_to: null,
  created_by_user_id: 'u_1',
  created_by_session_id: null,
  created_by_persona: null,
  status: 'open',
  kind: 'issue',
  parent_ticket_id: null,
  child_ticket_ids: [],
  context_refs: [],
  file_refs: [],
  related_sessions: [],
  acceptance_criteria: [],
  resolver_session_id: null,
  resolver_user_id: null,
  completion_notes: null,
  changed_files: [],
  knowledge_entry_ids: [],
  depends_on: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  resolved_at: null,
};

const PREVIEW: JiraPreview = {
  summary: 'Fix login timeout',
  sections: [
    {
      key: 'summary',
      heading: 'Summary',
      mode: 'rewrite',
      paragraphs: [{ text: 'Rewrite me', citations: ['sess_1'] }],
    },
    {
      key: 'evidence',
      heading: 'Evidence',
      mode: 'verbatim',
      paragraphs: [{ text: 'Verbatim text', citations: ['sess_2'] }],
    },
  ],
  citations: ['sess_1', 'sess_2'],
  issue_type: 'Story',
  jira_project_key: 'KEY',
  llm_used: true,
  key_source: 'claude',
  ticket_id: 't_1',
  project_id: 'proj_1',
};

beforeEach(() => {
  mockUseJiraLink.mockReset();
  mockUseJiraPreview.mockReset();
  mockUsePublishJira.mockReset();
  mockUseUnlinkJira.mockReset();
  addToast.mockReset();

  mockUseJiraLink.mockReturnValue({ data: null, isLoading: false });
  mockUseJiraPreview.mockReturnValue({ data: null, isLoading: false, isError: false, error: null });
  mockUsePublishJira.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUseUnlinkJira.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
});

describe('JiraTicketSection', () => {
  it('renders nothing for a project without a link or pending publish', () => {
    renderNode(
      <JiraTicketSection
        projectId="proj_1"
        ticket={baseTicket}
        orgId=""
        isProjectAdmin={false}
        isEnterprise={false}
      />,
    );
    expect(screen.queryByText('Jira')).not.toBeInTheDocument();
  });

  it('shows a linked badge with the issue key, live status and link', async () => {
    const ticket: Ticket = {
      ...baseTicket,
      jira: { issue_key: 'KEY-1', issue_url: 'https://acme.atlassian.net/browse/KEY-1', last_known_status: 'In Progress' },
    };
    mockUseJiraLink.mockReturnValue({ data: { last_known_status: 'Done' }, isLoading: false });
    renderNode(
      <JiraTicketSection projectId="proj_1" ticket={ticket} orgId="org_x" isProjectAdmin={false} isEnterprise />,
    );

    expect(screen.getByText('KEY-1')).toBeInTheDocument();
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://acme.atlassian.net/browse/KEY-1');
    expect(await screen.findByText('Done')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update Jira' })).toBeInTheDocument();
    // Non-admin has no Unlink control.
    expect(screen.queryByRole('button', { name: 'Unlink' })).not.toBeInTheDocument();
  });

  it('shows publishing and unknown states with their badge labels', () => {
    const ticket: Ticket = { ...baseTicket, jira_publish_in_progress: true, jira_publish_unknown: true };
    renderNode(
      <JiraTicketSection projectId="proj_1" ticket={ticket} orgId="org_x" isProjectAdmin={false} isEnterprise />,
    );

    expect(screen.getByText('publishing…')).toBeInTheDocument();
    expect(screen.getByText('publish result unknown')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Publish to reconcile' })).toBeInTheDocument();
  });

  it('shows Unlink only to project admins', () => {
    const ticket: Ticket = {
      ...baseTicket,
      jira: { issue_key: 'KEY-1', issue_url: 'https://x', last_known_status: null },
    };
    const { rerender } = renderNode(
      <JiraTicketSection projectId="proj_1" ticket={ticket} orgId="org_x" isProjectAdmin isEnterprise />,
    );
    expect(screen.getByRole('button', { name: 'Unlink' })).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={makeQueryClient()}>
        <JiraTicketSection projectId="proj_1" ticket={ticket} orgId="org_x" isProjectAdmin={false} isEnterprise />
      </QueryClientProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Unlink' })).not.toBeInTheDocument();
  });

  it('shows an upgrade hint instead of the Promote button for non-Enterprise users', () => {
    renderNode(
      <JiraTicketSection projectId="proj_1" ticket={baseTicket} orgId="org_x" isProjectAdmin={false} isEnterprise={false} />,
    );
    expect(screen.getByText('Promote to Jira is available on the Enterprise plan.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /promote to jira/i })).not.toBeInTheDocument();
  });

  it('renders a non-http issue URL as plain text instead of a link', () => {
    const ticket: Ticket = {
      ...baseTicket,
      jira: { issue_key: 'KEY-1', issue_url: 'javascript:alert(1)', last_known_status: null },
    };
    renderNode(
      <JiraTicketSection projectId="proj_1" ticket={ticket} orgId="org_x" isProjectAdmin={false} isEnterprise={false} />,
    );
    expect(screen.getByText('KEY-1')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});

describe('PromoteToJiraDialog', () => {
  it('renders an editable draft with citations, verbatim read-only sections and a model note', async () => {
    mockUseJiraPreview.mockReturnValue({ data: PREVIEW, isLoading: false, isError: false, error: null });
    const { container } = renderNode(
      <PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />,
    );

    expect(await screen.findByRole('textbox', { name: 'Summary' })).toHaveValue('Fix login timeout');
    // Model note mentions the key source.
    expect(screen.getByText(/drafted by your model \(claude\)/)).toBeInTheDocument();
    // Citation chips.
    expect(screen.getByText('sess_1')).toBeInTheDocument();
    expect(screen.getByText('sess_2')).toBeInTheDocument();
    // Rewritten section is editable; verbatim is read-only text.
    expect(screen.getByLabelText('Summary paragraph 1')).toHaveValue('Rewrite me');
    expect(screen.getByText('Verbatim text')).toBeInTheDocument();
    expect(screen.queryByLabelText('Evidence paragraph 1')).not.toBeInTheDocument();

    expect(await axe(container)).toHaveNoViolations();
  });

  it('publishes the edited summary and section text, not the original preview', async () => {
    mockUseJiraPreview.mockReturnValue({ data: PREVIEW, isLoading: false, isError: false, error: null });
    const mutateAsync = vi.fn().mockResolvedValue({ issue_key: 'KEY-1' });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    const onClose = vi.fn();
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={onClose} />);
    const user = userEvent.setup();

    const summary = await screen.findByRole('textbox', { name: 'Summary' });
    await user.clear(summary);
    await user.type(summary, 'New summary');

    const paragraph = screen.getByLabelText('Summary paragraph 1');
    await user.clear(paragraph);
    await user.type(paragraph, 'Edited text');

    await user.click(screen.getByRole('button', { name: 'Publish' }));

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledWith({
        ticketId: 't_1',
        body: {
          summary: 'New summary',
          sections: [
            {
              key: 'summary',
              heading: 'Summary',
              paragraphs: [{ text: 'Edited text', citations: ['sess_1'] }],
            },
            {
              key: 'evidence',
              heading: 'Evidence',
              paragraphs: [{ text: 'Verbatim text', citations: ['sess_2'] }],
            },
          ],
          confirm: true,
        },
      });
    });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(addToast).toHaveBeenCalledWith('success', 'Published to Jira');
  });

  it.each([
    ['jira_project_unmapped', 'Ask your admin to add a Jira mapping for this project.'],
    ['jira_not_connected', 'This organization has not connected Jira yet.'],
    ['upgrade_required', 'Jira promotion requires the Enterprise plan.'],
    ['insufficient_role', "You don't have permission to promote this ticket."],
    ['service_key_not_allowed', "You don't have permission to promote this ticket."],
    ['publish_in_progress', 'Another publish for this ticket is already in progress.'],
    ['jira_publish_unknown', 'A previous publish timed out and the Jira issue is not confirmed; publish again to reconcile.'],
    ['jira_link_stale', "This ticket's Jira link belongs to another organization or site; a project admin can unlink it first."],
    ['publish_superseded', 'This publish was superseded by a newer one; no link changed.'],
    ['link_changed', "This ticket's Jira link changed while publishing; no update was applied."],
    ['content_too_large', 'Some content is too large to publish to Jira Cloud.'],
    ['content_too_large_for_jira', 'Some content is too large to publish to Jira Cloud.'],
    ['jira_connection_failed', 'Jira rejected the stored connection; an admin should re-save the connection.'],
    ['jira_upstream_error', 'Jira is unreachable right now; try again shortly.'],
    ['jira_timeout', 'Jira is unreachable right now; try again shortly.'],
  ])('maps the %s error code to a clear message', async (code, message) => {
    mockUseJiraPreview.mockReturnValue({ data: null, isLoading: false, isError: true, error: apiError(code) });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);

    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it('surfaces a publish error message when the publish mutation rejects', async () => {
    mockUseJiraPreview.mockReturnValue({ data: PREVIEW, isLoading: false, isError: false, error: null });
    mockUsePublishJira.mockReturnValue({
      mutateAsync: vi.fn().mockRejectedValue(apiError('jira_project_unmapped')),
      isPending: false,
    });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);
    const user = userEvent.setup();

    await screen.findByRole('textbox', { name: 'Summary' });
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(await screen.findByText('Ask your admin to add a Jira mapping for this project.')).toBeInTheDocument();
  });

  it('clears the cached preview when the dialog closes', async () => {
    const removeSpy = vi.spyOn(QueryClient.prototype, 'removeQueries');
    mockUseJiraPreview.mockReturnValue({ data: PREVIEW, isLoading: false, isError: false, error: null });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);
    const user = userEvent.setup();

    await screen.findByRole('textbox', { name: 'Summary' });
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(removeSpy).toHaveBeenCalledWith({ queryKey: ['jiraPreview', 'proj_1', 't_1'] });
    removeSpy.mockRestore();
  });

  it('disables Publish while a fresh preview is fetching', async () => {
    mockUseJiraPreview.mockReturnValue({
      data: PREVIEW,
      isLoading: false,
      isError: false,
      error: null,
      isFetching: true,
    });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);

    await screen.findByRole('textbox', { name: 'Summary' });
    expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled();
  });
});
