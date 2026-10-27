import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import type { HelperCitationRef } from '../api/client';
import { axe } from '../test/axe';
import { AssistantMessage } from './HelperMessage';
import { citationAgeLabel, citationAgeSpoken, citationTimestamp, type Turn } from './format';

vi.mock('../hooks/useProjects', () => ({ useProject: () => ({ data: undefined }) }));

const actions = { onNewConversation: () => {}, onNavigate: () => {} };

function turn(content: string, citations: HelperCitationRef[], status: Turn['status'] = 'done'): Turn {
  return { id: 't1', role: 'assistant', content, toolSteps: [], citations, status };
}

function renderTurn(t: Turn) {
  return render(
    <MemoryRouter>
      <AssistantMessage turn={t} actions={actions} />
    </MemoryRouter>,
  );
}

const JIRA = {
  slug: 'integrations/jira',
  anchor: 'setup',
  title: 'Jira Cloud',
  url: 'https://sessionfs.dev/integrations/jira/#setup',
};

describe('doc citations in the drawer', () => {
  it('links a confirmed doc in a new tab and says so to screen readers', () => {
    renderTurn(turn('Set it up [doc:integrations/jira#setup].', [JIRA]));
    const chip = screen.getByRole('link', { name: 'Source 1: Jira Cloud (opens in new tab)' });
    expect(chip).toHaveAttribute('href', JIRA.url);
    expect(chip).toHaveAttribute('target', '_blank');
    const list = screen.getByRole('list', { name: 'Sources' });
    expect(within(list).getByRole('link', { name: '1. Jira Cloud (opens in new tab)' })).toHaveAttribute(
      'href',
      JIRA.url,
    );
  });

  it('renders an unconfirmed doc as an inert chip and leaves it out of Sources', () => {
    renderTurn(turn('See [doc:nope] for details.', []));
    expect(screen.queryAllByRole('link')).toHaveLength(0);
    expect(screen.getByTitle('Not found in the SessionFS docs')).toHaveTextContent('1');
    expect(screen.queryByRole('list', { name: 'Sources' })).toBeNull();
  });

  it('keeps a doc chip inert while streaming until the server confirms it', () => {
    renderTurn(turn('See [doc:quickstart] and more', [], 'streaming'));
    expect(screen.queryAllByRole('link')).toHaveLength(0);
    expect(screen.getByTitle('Checking source…')).toHaveTextContent('1');
  });
});

const PROJ = 'proj_1234567890abcdef';
const TICKET_ID = 'tk_1234567890abcdef';

const WIKI = {
  kind: 'wiki' as const,
  project_id: PROJ,
  slug: 'architecture',
  title: 'Architecture',
  at: '2026-10-10T08:30:00',
};

const TICKET = {
  kind: 'ticket' as const,
  ticket_id: TICKET_ID,
  project_id: PROJ,
  title: 'Fix login',
  at: '2026-10-10T08:30:00',
};

describe('ticket and wiki citations in the drawer', () => {
  it('links a confirmed wiki citation to the page itself and shows its age', () => {
    const PAGE = '/projects/proj_1234567890abcdef?tab=knowledge&sub=pages&page=architecture';
    renderTurn(turn('See [wiki:proj_1234567890abcdef/architecture].', [WIKI]));
    const chip = screen.getByRole('link', { name: 'Source 1: Architecture' });
    expect(chip).toHaveAttribute('href', PAGE);

    const list = screen.getByRole('list', { name: 'Sources' });
    // The age is part of the accessible name, not a replacement for it.
    const item = within(list).getByRole('link', {
      name: `1. Architecture, ${citationAgeSpoken(WIKI.at)}`,
    });
    expect(item).toHaveAttribute('href', PAGE);
    expect(within(list).getByText(citationAgeLabel(WIKI.at) ?? '')).toBeTruthy();
    expect(item).toHaveAttribute('title', `Architecture · ${citationTimestamp(WIKI.at)}`);
  });

  it('links a confirmed ticket citation to the expanded ticket and shows its age', () => {
    renderTurn(turn('See [ticket:tk_1234567890abcdef].', [TICKET]));
    const chip = screen.getByRole('link', { name: 'Source 1: Fix login' });
    expect(chip).toHaveAttribute(
      'href',
      '/projects/proj_1234567890abcdef?tab=work&sub=tickets&ticket=tk_1234567890abcdef',
    );

    const list = screen.getByRole('list', { name: 'Sources' });
    const item = within(list).getByRole('link', {
      name: `1. Fix login, ${citationAgeSpoken(TICKET.at)}`,
    });
    expect(item).toHaveAttribute(
      'href',
      '/projects/proj_1234567890abcdef?tab=work&sub=tickets&ticket=tk_1234567890abcdef',
    );
    expect(within(list).getByText(citationAgeLabel(TICKET.at) ?? '')).toBeTruthy();
    expect(item).toHaveAttribute('title', `Fix login · ${citationTimestamp(TICKET.at)}`);
  });

  it('renders an old session citation without a timestamp with no age badge', () => {
    renderTurn(turn('See [session:s_a].', [{ session_id: 's_a', message_index: null, title: 'A' }]));
    const list = screen.getByRole('list', { name: 'Sources' });
    const item = within(list).getByRole('link', { name: '1. A' });
    expect(within(list).queryByText('today')).toBeNull();
    expect(item).toHaveTextContent(/^1A$/);
    expect(item).toHaveAttribute('title', 'A');
  });

  it('renders an unparseable timestamp with no age badge', () => {
    renderTurn(turn('See [wiki:proj_1234567890abcdef/architecture].', [{ ...WIKI, at: 'garbage' }]));
    const list = screen.getByRole('list', { name: 'Sources' });
    const item = within(list).getByRole('link', { name: '1. Architecture' });
    expect(item).toHaveTextContent(/^1Architecture$/);
  });

  it('has no axe violations with ticket, wiki and project-less ticket chips', async () => {
    const { container } = renderTurn(
      turn('See [ticket:tk_1234567890abcdef], [wiki:proj_1234567890abcdef/architecture] and [ticket:tk_ffffffffffffffff].', [
        TICKET,
        WIKI,
        { ...TICKET, ticket_id: 'tk_ffffffffffffffff', project_id: null as unknown as string },
      ]),
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('renders a ticket citation without a project as an unlinked chip', () => {
    renderTurn(turn('See [ticket:tk_1234567890abcdef].', [{ ...TICKET, project_id: null as unknown as string }]));
    expect(screen.queryAllByRole('link')).toHaveLength(0);
    const list = screen.getByRole('list', { name: 'Sources' });
    // A plain span (no note role / aria-label) named by visually hidden text.
    expect(within(list).queryByRole('note')).toBeNull();
    const name = within(list).getByText(`1. Fix login, ${citationAgeSpoken(TICKET.at)}`);
    expect(name).toHaveClass('sr-only');
    expect(name.parentElement).not.toHaveAttribute('role');
    expect(name.parentElement).not.toHaveAttribute('aria-label');
    expect(name.parentElement).toHaveTextContent('Fix login');
  });
});
