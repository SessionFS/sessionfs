import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SearchResults from './SearchResults';

const mockUseSearch = vi.fn();
const mockNavigate = vi.fn();

vi.mock('../hooks/useSearch', () => ({
  useSearch: (...args: unknown[]) => mockUseSearch(...args),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

function renderSearchResults() {
  return render(
    <MemoryRouter initialEntries={['/search?q=debug']}>
      <Routes>
        <Route path="/search" element={<SearchResults />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('SearchResults', () => {
  beforeEach(() => {
    mockNavigate.mockReset();
    mockUseSearch.mockReset();
    mockUseSearch.mockImplementation((query: string, options?: { tool?: string; days?: number; page?: number }) => ({
      data: {
        results: [
          {
            session_id: 'ses_123',
            title: null,
            alias: 'Debug auth token refresh',
            source_tool: 'codex',
            model_id: 'gpt-5.4',
            message_count: 18,
            updated_at: '2026-04-06T12:00:00Z',
            matches: [{ snippet: 'debugged auth refresh flow' }],
          },
        ],
        total: 1,
        page: options?.page ?? 1,
        page_size: 20,
        query,
      },
      isLoading: false,
      error: null,
    }));
  });

  it('applies mobile filter sheet selections to the search query', async () => {
    const user = userEvent.setup();
    renderSearchResults();

    expect(mockUseSearch).toHaveBeenLastCalledWith('debug', {
      tool: undefined,
      days: undefined,
      page: 1,
    });

    await user.click(screen.getByRole('button', { name: /open search filters/i }));

    const dialog = screen.getByRole('dialog', { name: /refine results/i });

    // Select tool: open combobox, click option button
    await user.click(within(dialog).getByRole('combobox', { name: 'Tool' }));
    await user.click(
      within(within(dialog).getByRole('option', { name: 'Codex' })).getByRole('button'),
    );

    // Select date range: open combobox, click option button
    await user.click(within(dialog).getByRole('combobox', { name: 'Date range' }));
    await user.click(
      within(within(dialog).getByRole('option', { name: 'Last 30 days' })).getByRole('button'),
    );

    await user.click(within(dialog).getByRole('button', { name: /apply filters/i }));

    await waitFor(() => {
      expect(mockUseSearch).toHaveBeenLastCalledWith('debug', {
        tool: 'codex',
        days: 30,
        page: 1,
      });
    });

    const activeFilters = screen.getByLabelText('Active search filters');
    expect(within(activeFilters).getByText('Codex')).toBeInTheDocument();
    expect(within(activeFilters).getByText('Last 30 days')).toBeInTheDocument();
  });

  it('exposes Gemini and Copilot in the tool filters', async () => {
    const user = userEvent.setup();
    renderSearchResults();

    await user.click(screen.getByRole('button', { name: /open search filters/i }));
    const dialog = screen.getByRole('dialog', { name: /refine results/i });

    // Open the Tool combobox to reveal options
    await user.click(within(dialog).getByRole('combobox', { name: 'Tool' }));
    expect(within(dialog).getByRole('option', { name: 'Gemini CLI' })).toBeInTheDocument();
    expect(within(dialog).getByRole('option', { name: 'Copilot' })).toBeInTheDocument();
  });
});
