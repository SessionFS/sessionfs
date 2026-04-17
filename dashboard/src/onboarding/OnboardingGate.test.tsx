import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import OnboardingGate from './OnboardingGate';

const { mockUseQuery, mockGetItem, mockAuth } = vi.hoisted(() => ({
  mockUseQuery: vi.fn(),
  mockGetItem: vi.fn(),
  mockAuth: vi.fn(),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (...args: unknown[]) => mockUseQuery(...args),
}));

vi.mock('../utils/storage', () => ({
  getItem: (...args: unknown[]) => mockGetItem(...args),
  setItem: vi.fn(),
}));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => mockAuth(),
}));

describe('OnboardingGate', () => {
  beforeEach(() => {
    mockAuth.mockReturnValue({
      auth: { client: { listSessions: vi.fn(), listProjects: vi.fn() } },
    });
    mockGetItem.mockReturnValue(null);
    mockUseQuery.mockReset();
  });

  function renderGate() {
    return render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route
            path="/"
            element={
              <OnboardingGate>
                <div>Sessions Page</div>
              </OnboardingGate>
            }
          />
          <Route path="/getting-started" element={<div>Onboarding Page</div>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it('redirects to /getting-started when zero sessions and zero projects', () => {
    mockUseQuery.mockReturnValue({ data: null, isLoading: false });
    // First call = sessions (total 0), second call = projects (empty array)
    mockUseQuery
      .mockReturnValueOnce({ data: { total: 0 }, isLoading: false })
      .mockReturnValueOnce({ data: [], isLoading: false });

    renderGate();
    expect(screen.getByText('Onboarding Page')).toBeInTheDocument();
    expect(screen.queryByText('Sessions Page')).not.toBeInTheDocument();
  });

  it('renders children when user has sessions', () => {
    mockUseQuery
      .mockReturnValueOnce({ data: { total: 3 }, isLoading: false })
      .mockReturnValueOnce({ data: [], isLoading: false });

    renderGate();
    expect(screen.getByText('Sessions Page')).toBeInTheDocument();
  });

  it('renders children when onboarding-dismissed flag is set', () => {
    mockGetItem.mockReturnValue('1');
    // Even if queries would show 0/0, dismissed flag should skip redirect
    mockUseQuery.mockReturnValue({ data: null, isLoading: false });

    renderGate();
    expect(screen.getByText('Sessions Page')).toBeInTheDocument();
  });
});
