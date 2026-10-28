import type { ReactNode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type JiraPreview, type JiraTicketTemplates, type Ticket } from '../../api/client';
import { axe } from '../../test/axe';
import {
  defaultJiraTemplateChoice as defaultTemplateChoice,
  jiraPublishWarningMessage,
  jiraSiblingIssueUrl,
  jiraSprintDateLabel,
} from '../../utils/jira';
import { JiraTicketSection, PromoteToJiraDialog } from './JiraPromote';
import { TicketViewerContext } from './viewer';

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
const mockUseTicketJiraTemplates = vi.fn();
const mockUseLinkJiraIssue = vi.fn();
const mockUsePullJira = vi.fn();
const mockUseJiraSprints = vi.fn();
const mockUseTicketPreview = vi.fn();
const addToast = vi.fn();

vi.mock('../../hooks/useJiraTickets', () => ({
  useJiraLink: (...args: unknown[]) => mockUseJiraLink(...args),
  useJiraPreview: (...args: unknown[]) => mockUseJiraPreview(...args),
  usePublishJira: (...args: unknown[]) => mockUsePublishJira(...args),
  useUnlinkJira: (...args: unknown[]) => mockUseUnlinkJira(...args),
  useTicketJiraTemplates: (...args: unknown[]) => mockUseTicketJiraTemplates(...args),
  useLinkJiraIssue: (...args: unknown[]) => mockUseLinkJiraIssue(...args),
  usePullJira: (...args: unknown[]) => mockUsePullJira(...args),
  useJiraSprints: (...args: unknown[]) => mockUseJiraSprints(...args),
}));
vi.mock('../../hooks/useTickets', () => ({
  useTicketPreview: (...args: unknown[]) => mockUseTicketPreview(...args),
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

function renderNode(node: ReactNode) {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
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
  template: { slug: 'agile', name: 'Agile Standard', version: null },
};

const CHOICES: JiraTicketTemplates = {
  templates: [
    { slug: 'agile', name: 'Agile Standard', version: null, builtin: true },
    { slug: 'bug', name: 'Bug Report', version: null, builtin: true },
    { slug: 'design-lite', name: 'Design Lite', version: 3, builtin: false },
  ],
  inherited_default: { slug: 'agile', name: 'Agile Standard', version: null },
  pinned: null,
};

function choicesResult(data: JiraTicketTemplates | null = CHOICES) {
  return { data, isSuccess: !!data, isError: !data, refetch: vi.fn() };
}

beforeEach(() => {
  mockUseTicketJiraTemplates.mockReset();
  mockUseTicketJiraTemplates.mockReturnValue(choicesResult());
  mockUseJiraLink.mockReset();
  mockUseJiraPreview.mockReset();
  mockUsePublishJira.mockReset();
  mockUseUnlinkJira.mockReset();
  addToast.mockReset();

  mockUseJiraLink.mockReturnValue({ data: null, isLoading: false });
  mockUseJiraPreview.mockReturnValue({ data: null, isLoading: false, isError: false, error: null });
  mockUsePublishJira.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUseUnlinkJira.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUseLinkJiraIssue.mockReset();
  mockUseLinkJiraIssue.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUsePullJira.mockReset();
  mockUsePullJira.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  mockUseJiraSprints.mockReset();
  mockUseJiraSprints.mockReturnValue({ data: { sprints: [] }, isLoading: false, isError: false, error: null });
  mockUseTicketPreview.mockReset();
  mockUseTicketPreview.mockReturnValue({ data: undefined });
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

describe('Link existing issue', () => {
  const LINK_BUTTON = { name: 'Link existing issue' };

  function renderSection(overrides: Partial<Parameters<typeof JiraTicketSection>[0]> = {}) {
    return renderNode(
      <JiraTicketSection
        projectId="proj_1"
        ticket={baseTicket}
        orgId="org_x"
        isProjectAdmin={false}
        isEnterprise
        {...overrides}
      />,
    );
  }

  async function openForm() {
    const user = userEvent.setup();
    renderSection();
    await user.click(screen.getByRole('button', LINK_BUTTON));
    return { user, input: screen.getByRole('textbox', { name: 'Jira issue key' }) };
  }

  /** A link failure as the Jira routes envelope it. */
  function linkError(status: number, code: string, message = 'Conflict'): ApiError {
    return new ApiError(
      status,
      JSON.stringify({ error: { code: String(status), message, details: { error: code } } }),
    );
  }

  it('sits next to Promote for an unlinked ticket an editor can publish', () => {
    renderSection();
    expect(screen.getByRole('button', { name: 'Promote to Jira' })).toBeInTheDocument();
    const toggle = screen.getByRole('button', LINK_BUTTON);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it.each([
    ['a view-only reader', { canEdit: false }],
    ['a non-Enterprise project', { isEnterprise: false }],
    ['a linked ticket', { ticket: { ...baseTicket, jira: { issue_key: 'KEY-1', issue_url: 'https://x', last_known_status: null } } }],
    ['a publish in flight', { ticket: { ...baseTicket, jira_publish_in_progress: true } }],
    ['a publish awaiting reconciliation', { ticket: { ...baseTicket, jira_publish_unknown: true } }],
  ])('is not offered to %s', (_label, overrides) => {
    renderSection(overrides as Partial<Parameters<typeof JiraTicketSection>[0]>);
    expect(screen.queryByRole('button', LINK_BUTTON)).not.toBeInTheDocument();
  });

  it('opens an inline form with the key field focused, and Cancel returns focus', async () => {
    const { user, input } = await openForm();
    const toggle = screen.getByRole('button', LINK_BUTTON);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const form = screen.getByRole('form', { name: 'Link an existing Jira issue' });
    expect(toggle).toHaveAttribute('aria-controls', form.id);
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute('placeholder', 'PROJ-123');

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('form', { name: 'Link an existing Jira issue' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', LINK_BUTTON)).toHaveFocus());
  });

  it('closes on Escape', async () => {
    const { user } = await openForm();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('form', { name: 'Link an existing Jira issue' })).not.toBeInTheDocument();
  });

  it('checks the key shape before sending anything', async () => {
    const mutateAsync = vi.fn();
    mockUseLinkJiraIssue.mockReturnValue({ mutateAsync, isPending: false });
    const { user, input } = await openForm();

    await user.click(screen.getByRole('button', { name: 'Link issue' }));
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAccessibleDescription('Enter a Jira issue key.');

    await user.type(input, 'not a key');
    // Typing clears the error until the next attempt.
    expect(input).not.toHaveAttribute('aria-invalid');
    await user.keyboard('{Enter}');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAccessibleDescription(/like PROJ-123/);
    expect(input).toHaveFocus();
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it('accepts a pasted browse link, sends the bare key and closes on success', async () => {
    const mutateAsync = vi
      .fn()
      .mockResolvedValue({ issue_key: 'ABC-123', issue_url: 'https://acme.atlassian.net/browse/ABC-123' });
    mockUseLinkJiraIssue.mockReturnValue({ mutateAsync, isPending: false });
    const { user, input } = await openForm();

    await user.click(input);
    await user.paste('https://acme.atlassian.net/browse/abc-123');
    await user.click(screen.getByRole('button', { name: 'Link issue' }));

    expect(mockUseLinkJiraIssue).toHaveBeenCalledWith('proj_1');
    expect(mutateAsync).toHaveBeenCalledWith({ ticketId: 't_1', issueKey: 'ABC-123' });
    expect(addToast).toHaveBeenCalledWith('success', 'Linked ABC-123 on acme.atlassian.net');
    expect(screen.queryByRole('form', { name: 'Link an existing Jira issue' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('group', { name: 'Jira issue' })).toHaveFocus());
  });

  it('warns when a pasted link points at a different Jira site than the connected one', async () => {
    const mutateAsync = vi
      .fn()
      .mockResolvedValue({ issue_key: 'ABC-1', issue_url: 'https://acme.atlassian.net/browse/ABC-1' });
    mockUseLinkJiraIssue.mockReturnValue({ mutateAsync, isPending: false });
    const { user, input } = await openForm();
    await user.click(input);
    await user.paste('other.atlassian.net/jira/software/projects/ABC/issues/ABC-1');
    await user.keyboard('{Enter}');

    expect(mutateAsync).toHaveBeenCalledWith({ ticketId: 't_1', issueKey: 'ABC-1' });
    expect(addToast).toHaveBeenCalledWith(
      'warning',
      'Linked ABC-1 on acme.atlassian.net, this organization’s Jira site — not other.atlassian.net, where the pasted link points.',
    );
  });

  it('reports a ticket linked meanwhile and keeps focus in the section', async () => {
    mockUseLinkJiraIssue.mockReturnValue({
      mutateAsync: vi.fn().mockRejectedValue(linkError(409, 'jira_already_linked')),
      isPending: false,
    });
    const { user, input } = await openForm();
    await user.type(input, 'ABC-123{Enter}');

    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith('info', 'This ticket is already linked to a Jira issue.'),
    );
    expect(screen.queryByRole('form', { name: 'Link an existing Jira issue' })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('group', { name: 'Jira issue' })).toHaveFocus());
  });

  it('keeps the form (and the drawer) open on Escape while a link is in flight', async () => {
    mockUseLinkJiraIssue.mockReturnValue({ mutateAsync: vi.fn(), isPending: true });
    const { user } = await openForm();
    const outer = vi.fn();
    document.addEventListener('keydown', outer);
    await user.keyboard('{Escape}');
    document.removeEventListener('keydown', outer);
    expect(screen.getByRole('form', { name: 'Link an existing Jira issue' })).toBeInTheDocument();
    // The drawer's document listener either never sees the key or sees it handled.
    for (const [ev] of outer.mock.calls) expect((ev as KeyboardEvent).defaultPrevented).toBe(true);
  });

  it('shows the pending state while linking', async () => {
    mockUseLinkJiraIssue.mockReturnValue({ mutateAsync: vi.fn(), isPending: true });
    await openForm();
    expect(screen.getByRole('button', { name: 'Link issue' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'Jira issue key' })).toHaveAttribute('readonly');
  });

  it.each([
    [linkError(409, 'jira_issue_linked_elsewhere'), 'field', 'That issue is already linked to another ticket.'],
    [linkError(404, 'jira_issue_not_found'), 'field', 'No issue with that key, or the Jira connection can’t see it.'],
    [linkError(422, 'invalid_issue_key'), 'field', 'Enter a Jira issue key like PROJ-123, or paste the issue’s link.'],
    [
      new ApiError(
        422,
        JSON.stringify({ error: { code: '422', message: 'Validation error', details: { errors: [{ loc: ['body', 'issue_key'] }] } } }),
      ),
      'field',
      'Enter a Jira issue key like PROJ-123, or paste the issue’s link.',
    ],
    [
      linkError(422, 'jira_project_not_mapped', 'Jira project OPS is not mapped to this project (mapped: EMMS).'),
      'field',
      'Jira project OPS is not mapped to this project (mapped: EMMS).',
    ],
    [linkError(504, 'jira_timeout'), 'form', 'Jira is unreachable right now; try again shortly.'],
    [linkError(502, 'jira_upstream_error'), 'form', 'Jira is unreachable right now; try again shortly.'],
    [new ApiError(403, JSON.stringify({ error: { code: '403', message: 'Forbidden' } })), 'form', 'You don’t have permission to link this ticket.'],
    [
      // Shared editor gate: `{"code": "project_edit_forbidden", ...}` lands in the envelope's code.
      new ApiError(
        403,
        JSON.stringify({ error: { code: 'project_edit_forbidden', message: 'You can view but not edit this project' } }),
      ),
      'form',
      'You don’t have permission to link this ticket.',
    ],
    [linkError(409, 'jira_not_connected'), 'form', 'This organization has not connected Jira yet.'],
    [new Error('boom'), 'form', 'boom'],
  ])('maps a link failure (%#) to a friendly %s message', async (err, where, message) => {
    mockUseLinkJiraIssue.mockReturnValue({ mutateAsync: vi.fn().mockRejectedValue(err), isPending: false });
    const { user, input } = await openForm();
    await user.type(input, 'ABC-123{Enter}');

    if (where === 'field') {
      await waitFor(() => expect(input).toHaveAttribute('aria-invalid', 'true'));
      expect(input).toHaveAccessibleDescription(message);
    } else {
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(input).not.toHaveAttribute('aria-invalid');
    }
    // The form stays open with the key for another try.
    expect(input).toHaveValue('ABC-123');
    expect(input).toHaveFocus();
  });

  it('has no axe violations with the form open and an error showing', async () => {
    mockUseLinkJiraIssue.mockReturnValue({
      mutateAsync: vi.fn().mockRejectedValue(linkError(404, 'jira_issue_not_found')),
      isPending: false,
    });
    const user = userEvent.setup();
    const { container } = renderSection();
    await user.click(screen.getByRole('button', LINK_BUTTON));
    await user.type(screen.getByRole('textbox', { name: 'Jira issue key' }), 'ABC-1{Enter}');
    await screen.findByText(/No issue with that key/);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Linked ticket: choosing which side wins', () => {
  const LINKED: Ticket = {
    ...baseTicket,
    jira: { issue_key: 'KEY-1', issue_url: 'https://acme.atlassian.net/browse/KEY-1', last_known_status: 'To Do' },
  };
  const LINK_BODY = {
    ticket_id: 't_1',
    provider: 'jira',
    issue_key: 'KEY-1',
    issue_url: 'https://acme.atlassian.net/browse/KEY-1',
    last_pushed_at: null,
    last_known_status: 'To Do',
    last_status_checked_at: null,
    payload_hash: null,
  };
  const ATTACHED_PREVIEW: JiraPreview = {
    ...PREVIEW,
    existing: {
      issue_key: 'KEY-1',
      issue_url: 'https://acme.atlassian.net/browse/KEY-1',
      last_known_status: 'To Do',
      template_slug: null,
      template_version: null,
      attached: true,
    },
  };

  function renderLinked(attached: boolean, overrides: Partial<Parameters<typeof JiraTicketSection>[0]> = {}) {
    mockUseJiraLink.mockReturnValue({
      data: { ...LINK_BODY, attached_not_published: attached },
      isLoading: false,
    });
    return renderNode(
      <JiraTicketSection projectId="proj_1" ticket={LINKED} orgId="org_x" isProjectAdmin={false} isEnterprise {...overrides} />,
    );
  }

  it('offers a sync choice instead of Update Jira when the issue was linked from Jira', async () => {
    renderLinked(true);
    expect(screen.queryByRole('button', { name: 'Update Jira' })).not.toBeInTheDocument();
    // The menu holds the pull here; no duplicate button beside it.
    expect(screen.queryByRole('button', { name: 'Update from Jira' })).not.toBeInTheDocument();
    expect(screen.getByText('Linked from Jira — not published from SessionFS yet. Choose which side to keep when they differ.')).toBeInTheDocument();

    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: 'Sync with Jira…' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    await user.click(trigger);
    const menu = screen.getByRole('menu', { name: 'Sync with Jira' });
    expect(within(menu).getByRole('menuitem', { name: 'Overwrite KEY-1 in Jira…' })).toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: 'Overwrite this ticket from KEY-1…' })).toBeInTheDocument();
  });

  describe('who may take the Jira text (creator or project admin)', () => {
    function renderAs(
      userId: string,
      attached: boolean,
      isProjectAdmin = false,
    ) {
      mockUseJiraLink.mockReturnValue({ data: { ...LINK_BODY, attached_not_published: attached }, isLoading: false });
      return renderNode(
        <TicketViewerContext.Provider value={{ canEdit: true, userId }}>
          <JiraTicketSection projectId="proj_1" ticket={LINKED} orgId="org_x" isProjectAdmin={isProjectAdmin} isEnterprise />
        </TicketViewerContext.Provider>,
      );
    }

    it('gives a non-creator, non-admin editor only the Jira-side overwrite on an attached link', async () => {
      mockUseJiraPreview.mockReturnValue({ data: ATTACHED_PREVIEW, isLoading: false, isError: false, error: null });
      renderAs('u_other', true);
      expect(screen.queryByRole('button', { name: 'Sync with Jira…' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Update from Jira' })).not.toBeInTheDocument();
      await userEvent.setup().click(screen.getByRole('button', { name: 'Overwrite KEY-1 in Jira…' }));
      expect(await screen.findByRole('dialog', { name: 'Replace KEY-1 with this ticket' })).toBeInTheDocument();
    });

    it('hides Update from Jira on a published link from a non-creator, non-admin editor', () => {
      renderAs('u_other', false);
      expect(screen.getByRole('button', { name: 'Update Jira' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Update from Jira' })).not.toBeInTheDocument();
    });

    it.each([
      ['the creator', 'u_1', false],
      ['a project admin', 'u_other', true],
    ])('gives %s both directions and Update from Jira', (_who, userId, admin) => {
      const { unmount } = renderAs(userId, true, admin);
      expect(screen.getByRole('button', { name: 'Sync with Jira…' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Overwrite KEY-1 in Jira…' })).not.toBeInTheDocument();
      unmount();
      renderAs(userId, false, admin);
      expect(screen.getByRole('button', { name: 'Update from Jira' })).toBeInTheDocument();
    });
  });

  it('holds the Jira actions back until the live link has loaded', () => {
    mockUseJiraLink.mockReturnValue({ data: undefined, isLoading: true });
    renderNode(<JiraTicketSection projectId="proj_1" ticket={LINKED} orgId="org_x" isProjectAdmin={false} isEnterprise />);
    expect(screen.queryByRole('button', { name: 'Update Jira' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sync with Jira…' })).not.toBeInTheDocument();
  });

  it('gives a view-only reader no sync controls', () => {
    renderLinked(true, { canEdit: false });
    expect(screen.queryByRole('button', { name: 'Sync with Jira…' })).not.toBeInTheDocument();
    expect(screen.getByText('Linked from Jira — not published from SessionFS yet.')).toBeInTheDocument();
  });

  it('replacing the Jira issue opens the draft with a warning and sends overwrite_existing', async () => {
    mockUseJiraPreview.mockReturnValue({ data: ATTACHED_PREVIEW, isLoading: false, isError: false, error: null });
    const mutateAsync = vi.fn().mockResolvedValue({ issue_key: 'KEY-1' });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    renderLinked(true);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Sync with Jira…' }));
    await user.click(screen.getByRole('menuitem', { name: 'Overwrite KEY-1 in Jira…' }));

    const dialog = await screen.findByRole('dialog', { name: 'Replace KEY-1 with this ticket' });
    expect(within(dialog).getByText('This replaces KEY-1 in Jira')).toBeInTheDocument();
    expect(within(dialog).getByText('summary and description')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Replace KEY-1' }));

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        ticketId: 't_1',
        body: expect.objectContaining({ overwrite_existing: true, confirm: true }),
      }),
    );
    expect(addToast).toHaveBeenCalledWith('success', 'Replaced KEY-1 in Jira');
  });

  it.each([
    [['title', 'description'], 'success', 'Updated the title and description from KEY-1'],
    [['description'], 'success', 'Updated the description from KEY-1'],
    [[], 'info', 'No changes from Jira KEY-1'],
  ])('replacing the ticket pulls from Jira (%j changed)', async (fields, tone, toast) => {
    const mutateAsync = vi.fn().mockResolvedValue({ ticket: LINKED, jira: LINK_BODY, updated_fields: fields });
    mockUsePullJira.mockReturnValue({ mutateAsync, isPending: false });
    renderLinked(true);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Sync with Jira…' }));
    await user.click(screen.getByRole('menuitem', { name: 'Overwrite this ticket from KEY-1…' }));

    const dialog = await screen.findByRole('dialog', { name: 'Overwrite this ticket from KEY-1?' });
    expect(within(dialog).getByText('title and description')).toBeInTheDocument();
    expect(within(dialog).getByText(/A field that’s empty in Jira is left as it is/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Acceptance criteria, comments, status/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Replace title and description' }));

    expect(mockUsePullJira).toHaveBeenCalledWith('proj_1');
    expect(mutateAsync).toHaveBeenCalledWith('t_1');
    await waitFor(() => expect(addToast).toHaveBeenCalledWith(tone, toast));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('offers Update from Jira beside Update Jira on a published link', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ ticket: LINKED, jira: LINK_BODY, updated_fields: ['title'] });
    mockUsePullJira.mockReturnValue({ mutateAsync, isPending: false });
    renderLinked(false);
    expect(screen.getByRole('button', { name: 'Update Jira' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sync with Jira…' })).not.toBeInTheDocument();
    expect(screen.queryByText(/not published from SessionFS/)).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Update from Jira' }));
    const dialog = await screen.findByRole('dialog', { name: 'Update this ticket from KEY-1?' });
    await user.click(within(dialog).getByRole('button', { name: 'Replace title and description' }));
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Updated the title from KEY-1'));
  });

  it('hides Update from Jira from view-only readers', () => {
    renderLinked(false, { canEdit: false });
    expect(screen.queryByRole('button', { name: 'Update from Jira' })).not.toBeInTheDocument();
  });

  it.each([
    [apiError('jira_not_linked'), 'This ticket is no longer linked to a Jira issue.'],
    [apiError('jira_issue_not_found'), 'The Jira issue no longer exists, or the Jira connection can’t see it.'],
    [apiError('jira_timeout'), 'Jira is unreachable right now; try again shortly.'],
    [apiError('jira_upstream_error'), 'Jira is unreachable right now; try again shortly.'],
    [new ApiError(403, JSON.stringify({ error: { code: '403', message: 'Forbidden' } })), 'You don’t have permission to update this ticket.'],
  ])('explains a failed pull (%#)', async (err, message) => {
    mockUsePullJira.mockReturnValue({ mutateAsync: vi.fn().mockRejectedValue(err), isPending: false });
    renderLinked(false);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Update from Jira' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Replace title and description' }));
    expect(await within(dialog).findByText(message)).toBeInTheDocument();
  });

  it('has no axe violations with the sync menu open', async () => {
    const { container } = renderLinked(true);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Sync with Jira…' }));
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('PromoteToJiraDialog', () => {
  const PUBLISHED_PREVIEW: JiraPreview = {
    ...PREVIEW,
    existing: {
      issue_key: 'KEY-1',
      issue_url: 'https://acme.atlassian.net/browse/KEY-1',
      last_known_status: 'To Do',
      template_slug: 'agile',
      template_version: null,
    },
  };

  it('asks before overwriting when the server says the issue was linked from Jira', async () => {
    mockUseJiraPreview.mockReturnValue({ data: PUBLISHED_PREVIEW, isLoading: false, isError: false, error: null });
    const mutateAsync = vi
      .fn()
      .mockRejectedValueOnce(apiError('jira_overwrite_required'))
      .mockResolvedValueOnce({ issue_key: 'KEY-1' });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    const onClose = vi.fn();
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={onClose} />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Publish update' }));
    expect(mutateAsync.mock.calls[0][0].body).not.toHaveProperty('overwrite_existing');
    expect(await screen.findByText('This replaces KEY-1 in Jira')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Replace KEY-1' }));
    await waitFor(() => expect(mutateAsync.mock.calls[1][0].body).toMatchObject({ overwrite_existing: true }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('mentions the diff only when there is one', async () => {
    mockUseJiraPreview.mockReturnValue({ data: PUBLISHED_PREVIEW, isLoading: false, isError: false, error: null });
    const { unmount } = renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={vi.fn()} />);
    expect(await screen.findByText(/publishing updates the existing issue/)).toBeInTheDocument();
    expect(screen.queryByText(/as a diff against/)).not.toBeInTheDocument();
    unmount();

    mockUseJiraPreview.mockReturnValue({
      data: { ...PUBLISHED_PREVIEW, diff: ['-old', '+new'] },
      isLoading: false,
      isError: false,
      error: null,
    });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={vi.fn()} />);
    expect(await screen.findByText(/as a diff against what was last published/)).toBeInTheDocument();
  });

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
          template: 'agile',
          template_version: null,
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

  it('defaults the template picker to the inherited template and previews with it', async () => {
    mockUseJiraPreview.mockReturnValue({ data: PREVIEW, isLoading: false, isError: false, error: null });
    const { container } = renderNode(
      <PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />,
    );
    await screen.findByRole('textbox', { name: 'Summary' });
    expect(screen.getByRole('button', { name: /Template/ })).toHaveTextContent('Agile Standard');
    expect(mockUseJiraPreview).toHaveBeenLastCalledWith('proj_1', 't_1', true, 'agile');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('re-previews with the picked template and publishes its slug and version', async () => {
    mockUseJiraPreview.mockImplementation(
      (_p: string, _t: string, _enabled: boolean, template?: string) => ({
        data:
          template === 'design-lite'
            ? { ...PREVIEW, template: { slug: 'design-lite', name: 'Design Lite', version: 3 } }
            : PREVIEW,
        dataUpdatedAt: template === 'design-lite' ? 2 : 1,
        isLoading: false,
        isError: false,
        error: null,
      }),
    );
    const mutateAsync = vi.fn().mockResolvedValue({ issue_key: 'KEY-1' });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);
    const user = userEvent.setup();

    await screen.findByRole('textbox', { name: 'Summary' });
    await user.click(screen.getByRole('button', { name: /Template/ }));
    await user.click(screen.getByRole('option', { name: 'Design Lite v3' }));
    expect(mockUseJiraPreview).toHaveBeenLastCalledWith('proj_1', 't_1', true, 'design-lite');

    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0].body).toMatchObject({
      template: 'design-lite',
      template_version: 3,
    });
  });

  it('shows a rebuild notice instead of an error when the template changed before publish', async () => {
    const refetch = vi.fn();
    mockUseJiraPreview.mockReturnValue({
      data: PREVIEW, isLoading: false, isError: false, error: null, refetch,
    });
    mockUsePublishJira.mockReturnValue({
      mutateAsync: vi.fn().mockRejectedValue(apiError('template_changed')),
      isPending: false,
    });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);
    const user = userEvent.setup();

    await screen.findByRole('textbox', { name: 'Summary' });
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    expect(await screen.findByText(/The story template changed since this draft was built/)).toBeInTheDocument();
    expect(refetch).toHaveBeenCalled();
  });

  it('defaults to the pinned archived template, labelled, and publishes with it', async () => {
    mockUseTicketJiraTemplates.mockReturnValue(
      choicesResult({
        ...CHOICES,
        templates: [
          ...CHOICES.templates,
          { slug: 'old-tpl', name: 'Old', version: 2, builtin: false, archived: true },
        ],
        pinned: { slug: 'old-tpl', version: 2 },
      }),
    );
    mockUseJiraPreview.mockReturnValue({
      data: { ...PREVIEW, template: { slug: 'old-tpl', name: 'Old', version: 2 } },
      isLoading: false,
      isError: false,
      error: null,
    });
    const mutateAsync = vi.fn().mockResolvedValue({ issue_key: 'KEY-1' });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);
    const user = userEvent.setup();

    await screen.findByRole('textbox', { name: 'Summary' });
    expect(screen.getByRole('button', { name: /Template/ })).toHaveTextContent('Old v2 (archived)');
    expect(mockUseJiraPreview).toHaveBeenLastCalledWith('proj_1', 't_1', true, 'old-tpl');
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0].body).toMatchObject({ template: 'old-tpl', template_version: 2 });
  });

  it('falls back to the inherited draft when the template list is unavailable', async () => {
    mockUseTicketJiraTemplates.mockReturnValue(choicesResult(null));
    mockUseJiraPreview.mockReturnValue({ data: PREVIEW, isLoading: false, isError: false, error: null });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={() => {}} />);
    await screen.findByRole('textbox', { name: 'Summary' });
    expect(screen.queryByRole('button', { name: /Template/ })).not.toBeInTheDocument();
    expect(mockUseJiraPreview).toHaveBeenLastCalledWith('proj_1', 't_1', true, undefined);
  });
});

describe('defaultTemplateChoice', () => {
  it('prefers the pinned template while it is still selectable', () => {
    expect(defaultTemplateChoice({ ...CHOICES, pinned: { slug: 'design-lite', version: 2 } })).toBe(
      'design-lite',
    );
  });

  it('keeps an archived pinned template the server still lists for this ticket', () => {
    const choices = {
      ...CHOICES,
      templates: [
        ...CHOICES.templates,
        { slug: 'old-tpl', name: 'Old', version: 2, builtin: false, archived: true },
      ],
      pinned: { slug: 'old-tpl', version: 2 },
    };
    expect(defaultTemplateChoice(choices)).toBe('old-tpl');
  });

  it('falls back to the inherited default when the pin is no longer listed', () => {
    expect(defaultTemplateChoice({ ...CHOICES, pinned: { slug: 'gone', version: 1 } })).toBe('agile');
    expect(defaultTemplateChoice(CHOICES)).toBe('agile');
  });
});

describe('Jira sprint and parent on the ticket', () => {
  const linkedTicket: Ticket = {
    ...baseTicket,
    kind: 'task',
    parent_ticket_id: 't_parent',
    jira: { issue_key: 'KEY-2', issue_url: 'https://acme.atlassian.net/browse/KEY-2', last_known_status: 'To Do' },
  };
  const LINK = {
    ticket_id: 't_1',
    provider: 'jira',
    issue_key: 'KEY-2',
    issue_url: 'https://acme.atlassian.net/browse/KEY-2',
    last_pushed_at: null,
    last_known_status: 'To Do',
    last_status_checked_at: null,
    payload_hash: 'h',
  };

  function renderWith(link: Record<string, unknown>, onNavigateTicket = vi.fn()) {
    mockUseJiraLink.mockReturnValue({ data: { ...LINK, ...link }, isLoading: false });
    const view = renderNode(
      <JiraTicketSection
        projectId="proj_1"
        ticket={linkedTicket}
        orgId="org_x"
        isProjectAdmin={false}
        isEnterprise
        onNavigateTicket={onNavigateTicket}
      />,
    );
    return { ...view, onNavigateTicket };
  }

  it('shows the sprint with its state, and the parent issue and ticket', async () => {
    const { onNavigateTicket, container } = renderWith({
      sprint: { id: 7, name: 'Sprint 14', state: 'active' },
      parent: { key: 'KEY-1', ticket_id: 't_parent' },
    });
    const planning = screen.getByRole('group', { name: 'Jira planning' });
    expect(within(planning).getByText('Sprint 14')).toBeInTheDocument();
    expect(within(planning).getByText('Active')).toBeInTheDocument();
    const parentLink = within(planning).getByRole('link', { name: /KEY-1/ });
    expect(parentLink).toHaveAttribute('href', 'https://acme.atlassian.net/browse/KEY-1');
    expect(parentLink).toHaveAttribute('target', '_blank');

    await userEvent.setup().click(within(planning).getByRole('button', { name: /t_parent/ }));
    expect(onNavigateTicket).toHaveBeenCalledWith('t_parent');
    // Only the in-project ticket was opened in place; nothing was looked up.
    expect(mockUseTicketPreview).not.toHaveBeenCalled();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('names a sprint known only by id and leaves out an unknown state', () => {
    renderWith({ sprint: { id: 42, name: null, state: null }, parent: null });
    expect(screen.getByText('Sprint 42')).toBeInTheDocument();
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
    expect(screen.queryByText('Parent')).not.toBeInTheDocument();
  });

  it('shows only the Jira key when the parent ticket is not readable', () => {
    renderWith({ sprint: null, parent: { key: 'KEY-1', ticket_id: null } });
    expect(screen.getByRole('link', { name: /KEY-1/ })).toBeInTheDocument();
    expect(screen.queryByText('Sprint')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /open the parent ticket/ })).not.toBeInTheDocument();
  });

  it('links a parent ticket in another project once its project is known', () => {
    mockUseTicketPreview.mockReturnValue({ data: { project_id: 'proj_2', title: 'Epic' } });
    renderWith({ sprint: null, parent: { key: 'OTHER-9', ticket_id: 't_other' } });
    expect(mockUseTicketPreview).toHaveBeenCalledWith('t_other');
    expect(screen.getByRole('link', { name: /t_other/ })).toHaveAttribute(
      'href',
      '/projects/proj_2?tab=work&sub=tickets&ticket=t_other',
    );
  });

  it('opens a looked-up parent in place when it turns out to be in this project', async () => {
    mockUseTicketPreview.mockReturnValue({ data: { project_id: 'proj_1', title: 'Epic' } });
    const { onNavigateTicket } = renderWith({ sprint: null, parent: { key: 'KEY-9', ticket_id: 't_epic' } });
    expect(screen.queryByRole('link', { name: /t_epic/ })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: /t_epic/ }));
    expect(onNavigateTicket).toHaveBeenCalledWith('t_epic');
  });

  it('stays quiet without a sprint or parent', () => {
    renderWith({ sprint: null, parent: null });
    expect(screen.queryByRole('group', { name: 'Jira planning' })).not.toBeInTheDocument();
  });
});

describe('Publishing with a sprint', () => {
  const SPRINTS = {
    sprints: [
      { id: 12, name: 'Sprint 15', state: 'future', board_name: 'WID board', start_date: '2026-11-03T09:00:00Z', end_date: '2026-11-17T09:00:00Z' },
      { id: 11, name: 'Sprint 14', state: 'active', board_name: 'WID board', start_date: '2026-10-20T09:00:00Z', end_date: '2026-11-03T09:00:00Z' },
    ],
  };

  function openDialog(onClose = vi.fn(), orgId?: string) {
    mockUseJiraPreview.mockReturnValue({ data: PREVIEW, isLoading: false, isError: false, error: null });
    const view = renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" orgId={orgId} onClose={onClose} />);
    return { ...view, onClose };
  }

  it('lists No sprint first, then active sprints before future ones, and sends the pick', async () => {
    mockUseJiraSprints.mockReturnValue({ data: SPRINTS, isLoading: false, isError: false, error: null });
    const mutateAsync = vi.fn().mockResolvedValue({ issue_key: 'KEY-1', warnings: [] });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    const { container } = openDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Sprint' }));
    const options = screen.getAllByRole('option').map((o) => o.textContent ?? '');
    expect(options[0]).toMatch(/^No sprint/);
    expect(options[1]).toMatch(/^Sprint 14/);
    expect(options[2]).toMatch(/^Sprint 15/);
    expect(screen.getByRole('group', { name: 'Active sprints' })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    // Searchable by name.
    await user.type(screen.getByRole('combobox'), '15');
    expect(screen.getAllByRole('option')).toHaveLength(1);
    await user.click(screen.getByRole('option', { name: /Sprint 15/ }));

    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await waitFor(() => expect(mutateAsync.mock.calls[0][0].body).toMatchObject({ sprint_id: 12 }));
  });

  it('sends no sprint_id when No sprint is kept', async () => {
    mockUseJiraSprints.mockReturnValue({ data: SPRINTS, isLoading: false, isError: false, error: null });
    const mutateAsync = vi.fn().mockResolvedValue({ issue_key: 'KEY-1' });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    openDialog();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Publish' }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0].body).not.toHaveProperty('sprint_id');
  });

  it('says to leave the sprint alone when updating an existing issue', () => {
    mockUseJiraSprints.mockReturnValue({ data: SPRINTS, isLoading: false, isError: false, error: null });
    mockUseJiraPreview.mockReturnValue({
      data: { ...PREVIEW, existing: { issue_key: 'KEY-1', issue_url: 'https://acme.atlassian.net/browse/KEY-1', last_known_status: null, template_slug: 'agile', template_version: null } },
      isLoading: false,
      isError: false,
      error: null,
    });
    renderNode(<PromoteToJiraDialog projectId="proj_1" ticketId="t_1" onClose={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Sprint' })).toHaveTextContent('Don’t change the sprint');
  });

  it('explains when the site has no Jira Software', () => {
    mockUseJiraSprints.mockReturnValue({
      data: { sprints: [], unavailable: 'jira_software_not_available' },
      isLoading: false,
      isError: false,
      error: null,
    });
    openDialog();
    expect(screen.queryByRole('button', { name: 'Sprint' })).not.toBeInTheDocument();
    expect(screen.getByText(/doesn’t have Jira Software boards/)).toBeInTheDocument();
  });

  it('explains a connection without a sprint field and offers no picker', async () => {
    mockUseJiraSprints.mockReturnValue({
      data: { sprints: [], unavailable: 'jira_sprint_field_unknown' },
      isLoading: false,
      isError: false,
      error: null,
    });
    const { container } = openDialog();
    expect(screen.queryByRole('button', { name: 'Sprint' })).not.toBeInTheDocument();
    expect(screen.getByText(/doesn’t know the site’s sprint field/)).toHaveTextContent(/Ask an org admin to run Detect fields/);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('leaves the picker out for someone the sprints list refuses', () => {
    mockUseJiraSprints.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(403, JSON.stringify({ error: { code: '403', message: 'Forbidden' } })),
    });
    openDialog();
    expect(screen.queryByText('Sprint')).not.toBeInTheDocument();
  });

  it('shows every publish warning as a sentence and stays open until Done', async () => {
    mockUseJiraSprints.mockReturnValue({ data: SPRINTS, isLoading: false, isError: false, error: null });
    const mutateAsync = vi.fn().mockResolvedValue({
      issue_key: 'KEY-1',
      issue_url: 'https://acme.atlassian.net/browse/KEY-1',
      warnings: ['jira_sprint_not_set:move_failed', 'jira_parent_not_linked', 'jira_field_not_on_screen:duedate', 'jira_brand_new_thing'],
    });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    const { onClose, container } = openDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Publish' }));
    const dialog = await screen.findByRole('dialog', { name: 'Published KEY-1, with notes' });
    expect(within(dialog).getByText(/didn’t move it into the chosen sprint/)).toBeInTheDocument();
    expect(within(dialog).getByText(/parent isn’t linked to a Jira issue yet/)).toBeInTheDocument();
    expect(within(dialog).getByText(/due date wasn’t set: the field isn’t on this issue type’s screen/)).toBeInTheDocument();
    // An unknown code is shown as is rather than dropped.
    expect(within(dialog).getByText('jira_brand_new_thing')).toBeInTheDocument();
    expect(addToast).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(await axe(container)).toHaveNoViolations();

    await user.click(within(dialog).getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalled();
  });

  it('points at the connection settings when the org has no sprint field', async () => {
    mockUseJiraSprints.mockReturnValue({ data: SPRINTS, isLoading: false, isError: false, error: null });
    const err = new ApiError(
      422,
      JSON.stringify({ error: { code: '422', message: 'Unprocessable', details: { error: 'jira_sprint_field_unknown' } } }),
    );
    mockUsePublishJira.mockReturnValue({ mutateAsync: vi.fn().mockRejectedValue(err), isPending: false });
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries');
    openDialog(vi.fn(), 'org_x');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Sprint' }));
    await user.click(screen.getByRole('option', { name: /Sprint 14/ }));
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    expect(await screen.findByText('Jira sprint field unknown')).toBeInTheDocument();
    // Neutral wording: on a re-publish the option is "Don't change the sprint".
    expect(screen.getByText(/Clear the sprint to publish now/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Integrations → Jira → Connection/ })).toHaveAttribute(
      'href',
      '/settings/organization?tab=integrations&integration=jira&section=connection&org=org_x',
    );
    // The sprints list is refetched so the picker gives way to its note.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['jiraSprints', 'proj_1'] });
    invalidate.mockRestore();
  });

  it('links the org settings without an org when none is known', () => {
    mockUseJiraSprints.mockReturnValue({
      data: { sprints: [], unavailable: 'jira_sprint_field_unknown' },
      isLoading: false,
      isError: false,
      error: null,
    });
    openDialog();
    expect(screen.getByRole('link', { name: 'Open the Jira connection settings' })).toHaveAttribute(
      'href',
      '/settings/organization?tab=integrations&integration=jira&section=connection',
    );
  });

  it('leaves non-numeric sprint ids out of the picker and the request', async () => {
    mockUseJiraSprints.mockReturnValue({
      data: {
        sprints: [
          { id: 'abc', name: 'Odd sprint', state: 'active', board_name: null, start_date: null, end_date: null },
          { id: '12a', name: 'Mixed id', state: 'future', board_name: null, start_date: null, end_date: null },
          ...SPRINTS.sprints,
        ],
      },
      isLoading: false,
      isError: false,
      error: null,
    });
    const mutateAsync = vi.fn().mockResolvedValue({ issue_key: 'KEY-1' });
    mockUsePublishJira.mockReturnValue({ mutateAsync, isPending: false });
    openDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Sprint' }));
    expect(screen.queryByRole('option', { name: /Odd sprint/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Mixed id/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole('option')).toHaveLength(3);
    await user.keyboard('{Escape}');
    await user.click(screen.getByRole('button', { name: 'Publish' }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalled());
    expect(mutateAsync.mock.calls[0][0].body).not.toHaveProperty('sprint_id');
  });

  it('shows sprint dates as Jira calendar dates', async () => {
    mockUseJiraSprints.mockReturnValue({
      data: {
        sprints: [
          { id: 5, name: 'Late start', state: 'active', board_name: 'B', start_date: '2026-10-20T00:30:00.000Z', end_date: '2026-11-02T23:30:00.000Z' },
        ],
      },
      isLoading: false,
      isError: false,
      error: null,
    });
    openDialog();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Sprint' }));
    const label = `${jiraSprintDateLabel('2026-10-20T00:30:00.000Z')} – ${jiraSprintDateLabel('2026-11-02T23:30:00.000Z')}`;
    expect(screen.getByRole('option', { name: /Late start/ })).toHaveTextContent(label);
  });
});

describe('jiraPublishWarningMessage', () => {
  it.each([
    ['jira_sprint_not_set:field_rejected', /refused the sprint field/],
    ['jira_sprint_not_set:move_failed', /didn’t move it into the chosen sprint/],
    ['jira_sprint_not_set:other', /didn’t put it in the chosen sprint/],
    ['jira_parent_not_set', /didn’t accept the parent/],
    ['jira_parent_not_linked', /parent isn’t linked/],
    ['jira_field_not_on_screen:timetracking', /original estimate wasn’t set: the field isn’t on/],
    ['jira_invalid_value:duedate', /due date wasn’t set: Jira rejected its value/],
    ['jira_invalid_value:constructor', /constructor wasn’t set/],
  ])('%s', (code, expected) => {
    expect(jiraPublishWarningMessage(code)).toMatch(expected);
  });

  it('falls back to the raw code', () => {
    expect(jiraPublishWarningMessage('something_else:x')).toBe('something_else:x');
  });
});

describe('jiraSprintDateLabel', () => {
  it('formats the instant in the viewer’s local zone', () => {
    const local = (iso: string) =>
      new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    expect(jiraSprintDateLabel('2026-10-20T00:30:00Z')).toBe(local('2026-10-20T00:30:00Z'));
    expect(jiraSprintDateLabel('2026-11-02T23:30:00Z')).toBe(local('2026-11-02T23:30:00Z'));
    expect(jiraSprintDateLabel(null)).toBeNull();
    expect(jiraSprintDateLabel('not a date')).toBeNull();
  });
});

describe('jiraSiblingIssueUrl', () => {
  it('builds the browse URL on the linked issue’s site', () => {
    expect(jiraSiblingIssueUrl('https://acme.atlassian.net/browse/KEY-2', 'KEY-1')).toBe(
      'https://acme.atlassian.net/browse/KEY-1',
    );
  });

  it.each([
    ['javascript:alert(1)', 'KEY-1'],
    ['data:text/html,hi', 'KEY-1'],
    ['not a url', 'KEY-1'],
    ['', 'KEY-1'],
    ['https://acme.atlassian.net/browse/KEY-2', 'key-1'],
    ['https://acme.atlassian.net/browse/KEY-2', 'KEY-1/../../x'],
    ['https://acme.atlassian.net/browse/KEY-2', 'KEY'],
    ['https://acme.atlassian.net/browse/KEY-2', '"><script>'],
  ])('refuses %s with key %s', (url, key) => {
    expect(jiraSiblingIssueUrl(url, key)).toBeNull();
  });

  it('renders a parent with a bad issue URL as plain text', () => {
    mockUseJiraLink.mockReturnValue({
      data: {
        issue_key: 'KEY-2',
        issue_url: 'javascript:alert(1)',
        last_known_status: null,
        parent: { key: 'KEY-1', ticket_id: null },
        sprint: null,
      },
      isLoading: false,
    });
    renderNode(
      <JiraTicketSection
        projectId="proj_1"
        ticket={{ ...baseTicket, jira: { issue_key: 'KEY-2', issue_url: 'javascript:alert(1)', last_known_status: null } }}
        orgId="org_x"
        isProjectAdmin={false}
        isEnterprise
      />,
    );
    const planning = screen.getByRole('group', { name: 'Jira planning' });
    expect(within(planning).getByText('KEY-1')).toBeInTheDocument();
    expect(within(planning).queryByRole('link')).not.toBeInTheDocument();
  });
});
