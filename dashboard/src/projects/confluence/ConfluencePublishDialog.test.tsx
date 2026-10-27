import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, type ConfluenceLink, type ConfluencePreview } from '../../api/client';
import { axe } from '../../test/axe';
import { ConfluencePublishDialog } from './ConfluencePublishDialog';

/**
 * Project → wiki page / context → Publish to Confluence. Covers the draft
 * render (verbatim vs rewrite), editing a rewrite paragraph, the publish
 * payload (edited text + template echo), linked-page state, drift resolution
 * (diff + Overwrite / Stop syncing), unlink, notes and dropped paragraphs.
 */

const mockAuth = vi.fn();
const mockPreview = vi.fn();
const mockPublish = vi.fn();
const mockResolve = vi.fn();
const mockUnlink = vi.fn();
const addToast = vi.fn();

vi.mock('../../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function renderDialog(onClose = vi.fn(), canAdmin = true) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <ConfluencePublishDialog
        projectId="proj_a"
        kind="wiki_page"
        slug="architecture"
        canAdmin={canAdmin}
        onClose={onClose}
      />
    </QueryClientProvider>,
  );
  return { ...utils, onClose };
}

const LINK: ConfluenceLink = {
  id: 'cl_1',
  source_kind: 'wiki_page',
  source_key: 'architecture',
  page_id: '9001',
  page_url: 'https://acme.atlassian.net/wiki/spaces/DEV/pages/9001',
  version: 4,
  status: 'linked',
  publishing: false,
  stale: false,
  template_slug: 'reference',
  template_version: null,
  last_pushed_at: '2026-10-20T10:00:00Z',
  last_push_failed_at: null,
  last_push_failure: null,
};

function makePreview(overrides: Partial<ConfluencePreview> = {}): ConfluencePreview {
  return {
    title: 'Architecture',
    sections: [
      {
        key: 'overview',
        heading: 'Overview',
        mode: 'rewrite',
        paragraphs: [{ text: 'The API serves the dashboard.', citations: ['kb:12'] }],
      },
      {
        key: 'source',
        heading: 'Source',
        mode: 'verbatim',
        paragraphs: [{ text: 'API, worker and dashboard.', citations: [] }],
      },
    ],
    citations: ['kb:12'],
    llm_used: true,
    key_source: 'org',
    project_id: 'proj_a',
    source_kind: 'wiki_page',
    source_key: 'architecture',
    template: { slug: 'reference', name: 'Reference', version: null },
    dropped: [],
    notes: [],
    link: null,
    ...overrides,
  };
}

function apiError(status: number, details: Record<string, unknown>, message = 'conflict') {
  return new ApiError(status, JSON.stringify({ error: { code: String(status), message, details } }));
}

beforeEach(() => {
  mockAuth.mockReset();
  mockPreview.mockReset();
  mockPublish.mockReset();
  mockResolve.mockReset();
  mockUnlink.mockReset();
  addToast.mockReset();
  mockAuth.mockReturnValue({
    auth: {
      apiKey: 'sk_test',
      baseUrl: 'http://test.api',
      client: {
        previewConfluence: mockPreview,
        publishConfluence: mockPublish,
        resolveConfluenceLink: mockResolve,
        unlinkConfluence: mockUnlink,
      },
    },
  });
  mockPreview.mockResolvedValue(makePreview());
  mockPublish.mockResolvedValue({ page_id: '9001', page_url: 'javascript:alert(1)', version: 5 });
  mockResolve.mockResolvedValue({ id: 'cl_1', status: 'linked' });
  mockUnlink.mockResolvedValue(undefined);
});

describe('ConfluencePublishDialog', () => {
  it('renders the draft with verbatim and rewrite sections', async () => {
    renderDialog();
    expect(await screen.findByRole('heading', { name: 'Publish to Confluence' })).toBeInTheDocument();
    expect(await screen.findByLabelText('Title')).toHaveValue('Architecture');

    const overview = screen.getByRole('region', { name: 'Overview' });
    expect(within(overview).getByText('rewritten')).toBeInTheDocument();
    expect(within(overview).getByRole('textbox', { name: 'Overview paragraph 1' })).toHaveValue(
      'The API serves the dashboard.',
    );
    expect(within(overview).getByText('kb:12')).toBeInTheDocument();

    const source = screen.getByRole('region', { name: 'Source' });
    expect(within(source).getByText('verbatim')).toBeInTheDocument();
    expect(within(source).queryByRole('textbox')).not.toBeInTheDocument();
    expect(within(source).getByText('API, worker and dashboard.')).toBeInTheDocument();

    expect(mockPreview).toHaveBeenCalledWith('proj_a', { kind: 'wiki_page', slug: 'architecture' });
    // No link: no link panel.
    expect(screen.queryByRole('region', { name: 'Confluence page' })).not.toBeInTheDocument();
  });

  it('publishes the edited text and echoes the preview template', async () => {
    const user = userEvent.setup();
    mockPreview.mockResolvedValue(
      makePreview({ template: { slug: 'runbook', name: 'Ops runbook', version: 3 } }),
    );
    const { onClose } = renderDialog();
    const box = await screen.findByRole('textbox', { name: 'Overview paragraph 1' });
    await user.clear(box);
    await user.type(box, 'Edited overview.');
    await user.click(screen.getByRole('button', { name: 'Publish' }));

    await waitFor(() => expect(mockPublish).toHaveBeenCalledTimes(1));
    expect(mockPublish).toHaveBeenCalledWith('proj_a', {
      kind: 'wiki_page',
      slug: 'architecture',
      title: 'Architecture',
      sections: [
        { key: 'overview', heading: 'Overview', paragraphs: [{ text: 'Edited overview.', citations: ['kb:12'] }] },
        { key: 'source', heading: 'Source', paragraphs: [{ text: 'API, worker and dashboard.', citations: [] }] },
      ],
      confirm: true,
      template: 'runbook',
      template_version: 3,
    });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(addToast).toHaveBeenCalledWith('success', 'Published to Confluence');
  });

  it('sends a null template_version for built-in templates', async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(await screen.findByRole('button', { name: 'Publish' }));
    await waitFor(() => expect(mockPublish).toHaveBeenCalled());
    expect(mockPublish.mock.calls[0][1]).toMatchObject({ template: 'reference', template_version: null });
  });

  it('shows a friendly message when the template changed', async () => {
    const user = userEvent.setup();
    mockPublish.mockRejectedValue(apiError(409, { error: 'template_changed' }));
    renderDialog();
    await user.click(await screen.findByRole('button', { name: 'Publish' }));
    expect(await screen.findByText(/template changed since this draft was built/i)).toBeInTheDocument();
  });

  it('shows the linked page with an open link and unlinks it', async () => {
    const user = userEvent.setup();
    mockPreview.mockResolvedValue(
      makePreview({
        link: LINK,
        existing: { page_id: '9001', page_url: LINK.page_url!, version: 4, template_slug: 'reference', template_version: null },
      }),
    );
    renderDialog();
    expect(await screen.findByRole('heading', { name: 'Update Confluence page' })).toBeInTheDocument();
    const panel = screen.getByRole('region', { name: 'Confluence page' });
    expect(within(panel).getByText('Linked')).toBeInTheDocument();
    expect(within(panel).getByRole('link', { name: 'Open the page' })).toHaveAttribute('href', LINK.page_url);
    expect(screen.getByRole('button', { name: 'Publish update' })).toBeEnabled();

    await user.click(within(panel).getByRole('button', { name: 'Unlink' }));
    await waitFor(() => expect(mockUnlink).toHaveBeenCalledWith('proj_a', 'cl_1'));
    expect(mockResolve).not.toHaveBeenCalled();
  });

  it('explains an unconfirmed earlier publish', async () => {
    mockPreview.mockResolvedValue(makePreview({ link: { ...LINK, status: 'unknown' } }));
    renderDialog();
    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    expect(within(panel).getByText('Unconfirmed')).toBeInTheDocument();
    expect(within(panel).getByText(/publishing will adopt the page/i)).toBeInTheDocument();
    expect(within(panel).queryByRole('button')).not.toBeInTheDocument();
  });

  it('blocks publishing on a stale link and offers only Unlink', async () => {
    // The server's real shape: 200, the link flagged stale, no page URL and no
    // `existing` (publish keeps refusing it with 409 confluence_link_stale).
    mockPreview.mockResolvedValue(makePreview({ link: { ...LINK, stale: true, page_url: null } }));
    renderDialog();
    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    expect(within(panel).getByText('Stale link')).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: 'Unlink' })).toBeEnabled();
    expect(within(panel).queryByRole('button', { name: 'Overwrite Confluence' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled();
  });

  it('hides resolve and unlink from users without project-admin standing', async () => {
    mockPreview.mockResolvedValue(makePreview({ link: { ...LINK, status: 'drifted' } }));
    renderDialog(vi.fn(), false);
    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    expect(within(panel).getByText('Edited in Confluence')).toBeInTheDocument();
    expect(within(panel).queryByRole('button')).not.toBeInTheDocument();
    expect(within(panel).getByText(/Ask a project admin/)).toBeInTheDocument();
  });

  it('hides Unlink on a stale link from non-admins', async () => {
    mockPreview.mockResolvedValue(makePreview({ link: { ...LINK, stale: true, page_url: null } }));
    renderDialog(vi.fn(), false);
    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    expect(within(panel).queryByRole('button', { name: 'Unlink' })).not.toBeInTheDocument();
  });

  it('shows the drift diff from a 409 and overwrites Confluence', async () => {
    const user = userEvent.setup();
    mockPreview.mockResolvedValue(makePreview({ link: LINK }));
    mockPublish.mockRejectedValueOnce(
      apiError(409, {
        error: 'confluence_page_drifted',
        link_id: 'cl_1',
        page_id: '9001',
        stored_version: 4,
        remote_version: 6,
        diff: ['--- published', '+++ confluence', '-Old line', '+New line edited in Confluence'],
        diff_truncated: true,
      }),
    );
    renderDialog();
    await user.click(await screen.findByRole('button', { name: 'Publish' }));

    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    expect(within(panel).getByText('Edited in Confluence')).toBeInTheDocument();
    const diff = within(panel).getByLabelText('Changes made in Confluence');
    expect(diff).toHaveTextContent('+New line edited in Confluence');
    expect(diff).toHaveTextContent('-Old line');
    expect(within(panel).getByText(/version 4 → 6/)).toBeInTheDocument();
    expect(within(panel).getByText(/diff was truncated/i)).toBeInTheDocument();

    await user.click(within(panel).getByRole('button', { name: 'Overwrite Confluence' }));
    await waitFor(() => expect(mockResolve).toHaveBeenCalledWith('proj_a', 'cl_1', { action: 'overwrite' }));
    // The draft is rebuilt after the conflict is resolved.
    await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(2));
    expect(addToast).toHaveBeenCalledWith('success', expect.stringMatching(/publish again/i));
  });

  it('stops syncing a drifted link from the preview', async () => {
    const user = userEvent.setup();
    mockPreview.mockResolvedValue(makePreview({ link: { ...LINK, status: 'drifted' } }));
    renderDialog();
    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    expect(within(panel).getByText('Edited in Confluence')).toBeInTheDocument();
    await user.click(within(panel).getByRole('button', { name: 'Stop syncing' }));
    await waitFor(() => expect(mockResolve).toHaveBeenCalledWith('proj_a', 'cl_1', { action: 'unlink' }));
  });

  it('disables link actions while a publish claim is live', async () => {
    mockPreview.mockResolvedValue(makePreview({ link: { ...LINK, status: 'drifted', publishing: true } }));
    renderDialog();
    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    expect(within(panel).getByRole('button', { name: 'Overwrite Confluence' })).toBeDisabled();
    expect(within(panel).getByRole('button', { name: 'Stop syncing' })).toBeDisabled();
    expect(within(panel).getByText(/publish is in progress/i)).toBeInTheDocument();
  });

  it('reports a non-admin resolve attempt', async () => {
    const user = userEvent.setup();
    mockPreview.mockResolvedValue(makePreview({ link: { ...LINK, status: 'drifted' } }));
    mockResolve.mockRejectedValue(apiError(403, { error: 'insufficient_role' }, 'forbidden'));
    renderDialog();
    const panel = await screen.findByRole('region', { name: 'Confluence page' });
    await user.click(within(panel).getByRole('button', { name: 'Overwrite Confluence' }));
    expect(await within(panel).findByText(/only a project admin/i)).toBeInTheDocument();
  });

  it('renders notes and dropped paragraphs', async () => {
    mockPreview.mockResolvedValue(
      makePreview({
        llm_used: false,
        notes: ['No model key is configured; rewrite sections use the source text verbatim.'],
        dropped: [{ section: 'overview', text: 'An uncited claim about latency.', reason: 'missing citation' }],
      }),
    );
    renderDialog();
    expect(await screen.findByText(/No model key is configured/)).toBeInTheDocument();
    expect(screen.getByText('1 drafted paragraph was dropped')).toBeInTheDocument();
    expect(screen.getByText('An uncited claim about latency.')).toBeInTheDocument();
    expect(screen.getByText(/missing citation/)).toBeInTheDocument();
  });

  it('has no axe violations with a drifted link, notes and dropped paragraphs', async () => {
    const user = userEvent.setup();
    mockPreview.mockResolvedValue(
      makePreview({
        link: LINK,
        notes: ['No model key is configured; rewrite sections use the source text verbatim.'],
        dropped: [{ section: 'overview', text: 'An uncited claim.', reason: 'invalid citation' }],
      }),
    );
    mockPublish.mockRejectedValueOnce(
      apiError(409, { error: 'confluence_page_drifted', link_id: 'cl_1', diff: ['-a', '+b'], diff_truncated: false }),
    );
    const { baseElement } = renderDialog();
    await user.click(await screen.findByRole('button', { name: 'Publish' }));
    await screen.findByLabelText('Changes made in Confluence');
    expect(await axe(baseElement)).toHaveNoViolations();
  });
});
