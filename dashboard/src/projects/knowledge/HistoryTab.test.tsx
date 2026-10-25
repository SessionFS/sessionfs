import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import HistoryTab from './HistoryTab';

// One layer up from the component, so the test stays on the render and not
// react-query. RelativeDate renders through unchanged.
const { useCompilations } = vi.hoisted(() => ({ useCompilations: vi.fn() }));

vi.mock('../../hooks/useProjects', () => ({ useCompilations }));

describe('HistoryTab', () => {
  beforeEach(() => {
    useCompilations.mockReset();
  });

  it('shows the empty state before any compilation', () => {
    useCompilations.mockReturnValue({ data: { compilations: [] }, isLoading: false });
    render(<HistoryTab projectId="proj_abc" />);
    expect(screen.getByText(/no compilations yet/i)).toBeInTheDocument();
  });

  it('labels each compilation manual or scheduled with word counts', () => {
    useCompilations.mockReturnValue({
      data: {
        compilations: [
          {
            id: 1,
            entries_compiled: 3,
            compiled_at: '2026-10-23T10:00:00Z',
            trigger: 'scheduled',
            context_words_before: 120,
            context_words_after: 210,
          },
          {
            id: 2,
            entries_compiled: 1,
            compiled_at: '2026-10-22T09:00:00Z',
            trigger: 'manual',
            context_words_before: 90,
            context_words_after: 95,
          },
        ],
      },
      isLoading: false,
    });

    render(<HistoryTab projectId="proj_abc" />);

    expect(screen.getByText(/3 entries compiled/i)).toBeInTheDocument();
    expect(screen.getByText(/scheduled/i)).toBeInTheDocument();
    expect(screen.getByText(/manual/i)).toBeInTheDocument();
    expect(screen.getByText(/120 words → 210 words/i)).toBeInTheDocument();
    expect(screen.getByText(/90 words → 95 words/i)).toBeInTheDocument();
  });
});
