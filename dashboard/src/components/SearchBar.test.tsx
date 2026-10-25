import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SearchBar from './SearchBar';
import { axe } from '../test/axe';

/**
 * Focused coverage for the "Ask the helper" command that lives in the search
 * palette: focusing the empty box (or pressing Cmd/Ctrl+K) reveals the command,
 * and choosing it hands off to the helper drawer via the HelperProvider context.
 * Search results themselves are covered by useSearch's own suite.
 */

const { useSearchMock, openHelperMock } = vi.hoisted(() => ({
  useSearchMock: vi.fn(),
  openHelperMock: vi.fn(),
}));

vi.mock('../hooks/useSearch', () => ({ useSearch: useSearchMock }));
vi.mock('../helper/HelperProvider', () => ({ useHelper: () => ({ openHelper: openHelperMock }) }));

function renderBar() {
  return render(
    <MemoryRouter>
      <SearchBar />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useSearchMock.mockReturnValue({ data: { results: [], total: 0 }, isLoading: false });
  openHelperMock.mockReset();
});

describe('SearchBar "Ask the helper" command', () => {
  it('reveals the command when the empty search box is focused', async () => {
    renderBar();
    const user = userEvent.setup();

    await user.click(screen.getByRole('combobox', { name: 'Search sessions' }));

    expect(screen.getByRole('option', { name: 'Ask the helper…' })).toBeInTheDocument();
  });

  it('opens the helper drawer and closes the palette when the command is clicked', async () => {
    renderBar();
    const user = userEvent.setup();

    await user.click(screen.getByRole('combobox', { name: 'Search sessions' }));
    await user.click(screen.getByRole('option', { name: 'Ask the helper…' }));

    expect(openHelperMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('option', { name: 'Ask the helper…' })).not.toBeInTheDocument();
  });

  it('opens the helper drawer when Enter is pressed on the empty box', async () => {
    renderBar();
    const user = userEvent.setup();

    await user.click(screen.getByRole('combobox', { name: 'Search sessions' }));
    await user.keyboard('{Enter}');

    expect(openHelperMock).toHaveBeenCalledTimes(1);
  });

  it('reveals the command via the Cmd/Ctrl+K shortcut', async () => {
    renderBar();
    const user = userEvent.setup();

    await user.keyboard('{Meta>}k{/Meta}');

    expect(screen.getByRole('option', { name: 'Ask the helper…' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Search sessions' })).toHaveFocus();
  });

  it('activates the helper command from the keyboard when the option has focus', async () => {
    renderBar();
    const user = userEvent.setup();

    await user.click(screen.getByRole('combobox', { name: 'Search sessions' }));
    const option = screen.getByRole('option', { name: 'Ask the helper…' });
    option.focus();
    await user.keyboard('{Enter}');

    expect(openHelperMock).toHaveBeenCalledTimes(1);
  });

  it('exposes the helper command as a listbox option with no nested button', async () => {
    renderBar();
    const user = userEvent.setup();

    await user.click(screen.getByRole('combobox', { name: 'Search sessions' }));

    const listbox = screen.getByRole('listbox', { name: 'Commands' });
    const option = within(listbox).getByRole('option', { name: 'Ask the helper…' });
    // aria wiring: it is a selected option inside the Commands listbox, and it
    // is activatable in its own right rather than wrapping a button.
    expect(option).toHaveAttribute('aria-selected', 'true');
    expect(within(option).queryByRole('button')).toBeNull();

    expect(await axe(listbox)).toHaveNoViolations();
  });
});
