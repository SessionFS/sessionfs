import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Loop } from '../api/reviewLoop';
import { loops, story } from '../projects/loop/fixtures';
import { StoryView } from '../projects/loop/StoryView';
import { LoopBoard } from '../projects/loop/LoopBoard';
import { axe } from '../test/axe';

const hooks = vi.hoisted(() => ({
  useStory: vi.fn(),
  useTicketSpend: vi.fn(),
  useChecklist: vi.fn(),
  useUpdateLoop: vi.fn(),
  useCloseLoopItem: vi.fn(),
  useSeedChecklist: vi.fn(),
  usePatchChecklistItem: vi.fn(),
  useLoopInsights: vi.fn(),
}));
vi.mock('../hooks/useReviewLoop', () => hooks);

const ok = (data: unknown) => ({ data, isLoading: false, error: null });

function withApprovals(): Loop[] {
  const l = loops();
  l[0].children[0] = {
    ...l[0].children[0],
    ready: false,
    not_ready_reasons: ['Waiting on approval: Auth changes (1 of 2)'],
    approvals: [
      {
        request_id: 'ar_5',
        policy_id: 'ap_1',
        policy_name: 'Auth changes',
        state: 'pending',
        blocking: true,
        approved_count: 1,
        required_count: 2,
      },
      {
        request_id: 'ar_6',
        policy_id: 'ap_2',
        policy_name: 'Style review',
        state: 'approved',
        blocking: false,
        approved_count: 1,
        required_count: 1,
      },
    ],
    warnings: ['Note: Docs review is advisory'],
  };
  return l;
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useStory.mockReturnValue(ok(story()));
  hooks.useTicketSpend.mockReturnValue(ok(undefined));
});

describe('Story and board approvals', () => {
  it('lists every matching policy on the Story with links and quiet warnings', async () => {
    const l = withApprovals();
    const id = l[0].children[0].ticket_id;
    const { container } = render(
      <MemoryRouter>
        <main>
          <StoryView projectId="proj_1" ticketId={id} loops={l} onBack={() => {}} />
        </main>
      </MemoryRouter>,
    );
    const panel = screen.getByRole('region', { name: 'Approvals' });
    expect(panel).toHaveTextContent('Auth changes');
    expect(panel).toHaveTextContent('1 of 2');
    expect(panel).toHaveTextContent('Style review');
    expect(within(panel).getAllByRole('link', { name: 'View request' })[0]).toHaveAttribute(
      'href',
      '/approvals?request=ar_5',
    );
    expect(panel).toHaveTextContent('Note: Docs review is advisory');
    expect(container).toBeTruthy();
    expect(await axe(document.body)).toHaveNoViolations();
  });

  it('shows Awaiting approval on the loop board card', () => {
    hooks.useLoopInsights.mockReturnValue(ok(undefined));
    render(
      <MemoryRouter>
        <main>
          <LoopBoard projectId="proj_1" loops={withApprovals()} />
        </main>
      </MemoryRouter>,
    );
    const lines = screen.getAllByRole('list', { name: 'Awaiting approval' });
    expect(lines[0]).toHaveTextContent('Awaiting approval: Auth changes (1 of 2)');
  });
});
