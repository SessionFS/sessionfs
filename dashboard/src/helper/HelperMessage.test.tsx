import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import type { HelperCitationRef } from '../api/client';
import { AssistantMessage } from './HelperMessage';
import type { Turn } from './format';

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
