import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import LoginPage from './LoginPage';

const { mockLogin, mockSignup, mockNavigate } = vi.hoisted(() => ({
  mockLogin: vi.fn(),
  mockSignup: vi.fn(),
  mockNavigate: vi.fn(),
}));

vi.mock('./AuthContext', () => ({
  useAuth: () => ({
    login: mockLogin,
  }),
}));

vi.mock('../api/client', () => ({
  signup: (...args: unknown[]) => mockSignup(...args),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(msg: string, status: number) {
      super(msg);
      this.status = status;
    }
  },
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

describe('LoginPage', () => {
  beforeEach(() => {
    mockLogin.mockReset();
    mockSignup.mockReset();
    mockNavigate.mockReset();
  });

  it('renders the updated product identity copy', () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    expect(screen.getByLabelText('SessionFS')).toBeInTheDocument();
    expect(
      screen.getByText(/resume work, build project memory, and hand off ai sessions without losing context/i),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });

  it('signup auto-authenticates and navigates to /getting-started', async () => {
    const user = userEvent.setup();
    mockSignup.mockResolvedValue({ raw_key: 'sk_sfs_test123' });
    mockLogin.mockResolvedValue(undefined);

    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>,
    );

    // Switch to signup mode
    await user.click(screen.getByRole('button', { name: /sign up/i }));

    // Fill in email
    const emailInput = screen.getByPlaceholderText('you@example.com');
    await user.type(emailInput, 'test@example.com');

    // Submit
    await user.click(screen.getByRole('button', { name: /create account/i }));

    await waitFor(() => {
      expect(mockSignup).toHaveBeenCalled();
      expect(mockLogin).toHaveBeenCalledWith(expect.any(String), 'sk_sfs_test123');
      expect(mockNavigate).toHaveBeenCalledWith('/getting-started');
    });
  });
});
