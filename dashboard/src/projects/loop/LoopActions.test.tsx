import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/client';
import { checklist, loops } from './fixtures';
import { ChecklistPanel } from './LoopActions';
import { NeedsYou } from './Panels';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useChecklist: vi.fn(),
    useUpdateLoop: vi.fn(),
    useCloseLoopItem: vi.fn(),
    useSeedChecklist: vi.fn(),
    usePatchChecklistItem: vi.fn(),
  },
}));
vi.mock('../../hooks/useReviewLoop', () => hooks);

const mut = (over: Record<string, unknown> = {}) => ({ mutate: vi.fn(), mutateAsync: vi.fn().mockResolvedValue({}), isPending: false, error: null, ...over });

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => { resolve = r; });
  return { promise, resolve };
}

function renderNeeds(withUsd: boolean, canEdit = true) {
  return render(
    <MemoryRouter>
      <NeedsYou projectId="proj_1" loops={loops(withUsd)} canEdit={canEdit} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useUpdateLoop.mockReturnValue(mut());
  hooks.useCloseLoopItem.mockReturnValue(mut());
});

describe('needs_human actions', () => {
  it('offers Raise limit and Close item to editors only, and only on a needs_human item', () => {
    renderNeeds(true, false);
    expect(screen.queryByRole('button', { name: /Raise limit/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Close item/ })).not.toBeInTheDocument();
  });

  it('raises the token and round limits for the whole loop', async () => {
    const update = mut();
    hooks.useUpdateLoop.mockReturnValue(update);
    renderNeeds(false);
    expect(screen.getAllByRole('button', { name: /Raise limit/ })).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', { name: /Raise limit/ }));
    const dlg = screen.getByRole('dialog', { name: 'Raise limit' });
    // No dollar field when the response carries no dollars.
    expect(within(dlg).queryByLabelText('Cost limit (USD)')).not.toBeInTheDocument();
    await userEvent.type(within(dlg).getByLabelText('Token limit'), '800000');
    await userEvent.type(within(dlg).getByLabelText('Review round limit'), '6');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Raise limit' }));
    expect(update.mutateAsync).toHaveBeenCalledWith({
      loopId: 'tk_parent',
      body: { token_budget: 800000, max_review_rounds: 6 },
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows the cost limit only when dollars are present, and sends it', async () => {
    const update = mut();
    hooks.useUpdateLoop.mockReturnValue(update);
    renderNeeds(true);
    await userEvent.click(screen.getByRole('button', { name: /Raise limit/ }));
    const dlg = screen.getByRole('dialog', { name: 'Raise limit' });
    await userEvent.type(within(dlg).getByLabelText('Cost limit (USD)'), '9.5');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Raise limit' }));
    expect(update.mutateAsync).toHaveBeenCalledWith({ loopId: 'tk_parent', body: { cost_budget_usd: 9.5 } });
  });

  it('rejects an empty or invalid limit without calling the server', async () => {
    const update = mut();
    hooks.useUpdateLoop.mockReturnValue(update);
    renderNeeds(false);
    await userEvent.click(screen.getByRole('button', { name: /Raise limit/ }));
    const dlg = screen.getByRole('dialog', { name: 'Raise limit' });
    await userEvent.click(within(dlg).getByRole('button', { name: 'Raise limit' }));
    expect(within(dlg).getByText('Enter at least one new limit.')).toBeInTheDocument();
    await userEvent.type(within(dlg).getByLabelText('Token limit'), '0');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Raise limit' }));
    expect(within(dlg).getByText(/Token limit must be a whole number/)).toBeInTheDocument();
    expect(update.mutateAsync).not.toHaveBeenCalled();
  });

  it('locks Escape, backdrop and Cancel while the limit write is pending', async () => {
    const d = deferred();
    hooks.useUpdateLoop.mockReturnValue(mut({ mutateAsync: vi.fn().mockReturnValue(d.promise), isPending: true }));
    renderNeeds(false);
    await userEvent.click(screen.getByRole('button', { name: /Raise limit/ }));
    const dlg = screen.getByRole('dialog', { name: 'Raise limit' });
    expect(within(dlg).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    await userEvent.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: 'Raise limit' })).toBeInTheDocument();
    // Backdrop is the first child of the overlay wrapper.
    await userEvent.click(dlg.previousElementSibling as HTMLElement);
    expect(screen.getByRole('dialog', { name: 'Raise limit' })).toBeInTheDocument();
    d.resolve();
  });

  it('keeps the dialog open with the error after a failed write', async () => {
    hooks.useUpdateLoop.mockReturnValue(
      mut({ mutateAsync: vi.fn().mockRejectedValue(new ApiError(403, 'forbidden')), error: new ApiError(403, '{"detail":"Only org owners may change dollar budgets"}') }),
    );
    renderNeeds(false);
    await userEvent.click(screen.getByRole('button', { name: /Raise limit/ }));
    const dlg = screen.getByRole('dialog', { name: 'Raise limit' });
    await userEvent.type(within(dlg).getByLabelText('Token limit'), '5');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Raise limit' }));
    expect(screen.getByRole('dialog', { name: 'Raise limit' })).toBeInTheDocument();
    expect(within(dlg).getByRole('alert')).toBeInTheDocument();
    // Escape works again once nothing is pending.
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('closes a needs_human item and dismisses the dialog on success', async () => {
    const close = mut();
    hooks.useCloseLoopItem.mockReturnValue(close);
    renderNeeds(false);
    await userEvent.click(screen.getByRole('button', { name: /Close item/ }));
    const dlg = screen.getByRole('dialog', { name: 'Close item' });
    await userEvent.click(within(dlg).getByRole('button', { name: 'Close item' }));
    expect(close.mutateAsync).toHaveBeenCalledWith({ loopId: 'tk_parent', ticketId: 'tk_child2' });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('locks the close dialog while pending and shows an error afterwards', async () => {
    hooks.useCloseLoopItem.mockReturnValue(mut({ isPending: true }));
    const { unmount } = renderNeeds(false);
    await userEvent.click(screen.getByRole('button', { name: /Close item/ }));
    const dlg = screen.getByRole('dialog', { name: 'Close item' });
    expect(within(dlg).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    await userEvent.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: 'Close item' })).toBeInTheDocument();
    unmount();

    hooks.useCloseLoopItem.mockReturnValue(
      mut({ mutateAsync: vi.fn().mockRejectedValue(new Error('x')), error: new ApiError(409, '{"detail":"Only a needs_human loop item can be closed by hand"}') }),
    );
    renderNeeds(false);
    await userEvent.click(screen.getByRole('button', { name: /Close item/ }));
    const dlg2 = screen.getByRole('dialog', { name: 'Close item' });
    await userEvent.click(within(dlg2).getByRole('button', { name: 'Close item' }));
    expect(within(dlg2).getByRole('alert')).toHaveTextContent(/Only a needs_human loop item/);
    expect(screen.getByRole('dialog', { name: 'Close item' })).toBeInTheDocument();
  });
});

describe('Checklist panel', () => {
  function setList(items: unknown) {
    hooks.useChecklist.mockReturnValue({ data: items, isLoading: false, error: null });
  }

  it('lists items by status, with Accept and Retire for editors', async () => {
    const patch = mut();
    setList(checklist());
    hooks.useSeedChecklist.mockReturnValue(mut());
    hooks.usePatchChecklistItem.mockReturnValue(patch);
    render(<ChecklistPanel projectId="proj_1" canEdit />);
    const proposed = screen.getByRole('list', { name: 'Proposed checklist items' });
    const active = screen.getByRole('list', { name: 'Active checklist items' });
    const retired = screen.getByRole('list', { name: 'Retired checklist items' });
    expect(within(retired).queryByRole('button')).not.toBeInTheDocument();
    expect(within(active).queryByRole('button', { name: /Accept/ })).not.toBeInTheDocument();
    expect(within(active).getByRole('button', { name: /Retire/ })).toBeInTheDocument();
    await userEvent.click(within(proposed).getByRole('button', { name: /Accept/ }));
    expect(patch.mutateAsync).toHaveBeenCalledWith({ itemId: 'lci_1', status: 'active' });
    await userEvent.click(within(active).getByRole('button', { name: /Retire/ }));
    expect(patch.mutateAsync).toHaveBeenCalledWith({ itemId: 'lci_2', status: 'retired' });
  });

  it('is read-only without edit access', () => {
    setList(checklist());
    hooks.useSeedChecklist.mockReturnValue(mut());
    hooks.usePatchChecklistItem.mockReturnValue(mut());
    render(<ChecklistPanel projectId="proj_1" canEdit={false} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('disables only the busy item while its write is in flight, then shows an error on failure', async () => {
    const d = deferred();
    const patch = mut({ mutateAsync: vi.fn().mockReturnValueOnce(d.promise.then(() => { throw new ApiError(404, '{"detail":"Checklist item not found"}'); })) });
    setList(checklist());
    hooks.useSeedChecklist.mockReturnValue(mut());
    hooks.usePatchChecklistItem.mockReturnValue(patch);
    render(<ChecklistPanel projectId="proj_1" canEdit />);
    const accept = screen.getByRole('button', { name: /Accept/ });
    await userEvent.click(accept);
    expect(accept).toBeDisabled();
    expect(screen.getByRole('button', { name: /Retire: Existence/ })).toBeEnabled();
    d.resolve();
    expect(await screen.findByRole('alert')).toHaveTextContent('Checklist item not found');
    expect(screen.getByRole('button', { name: /Accept/ })).toBeEnabled();
  });

  it('offers Seed when empty, only to editors', async () => {
    const seed = mut();
    setList([]);
    hooks.useSeedChecklist.mockReturnValue(seed);
    hooks.usePatchChecklistItem.mockReturnValue(mut());
    const { unmount } = render(<ChecklistPanel projectId="proj_1" canEdit />);
    await userEvent.click(screen.getByRole('button', { name: 'Seed checklist' }));
    expect(seed.mutate).toHaveBeenCalled();
    unmount();
    render(<ChecklistPanel projectId="proj_1" canEdit={false} />);
    expect(screen.queryByRole('button', { name: 'Seed checklist' })).not.toBeInTheDocument();
    expect(screen.getByText('No checklist items yet.')).toBeInTheDocument();
  });
});
