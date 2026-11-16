import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ThemeToggle from './ThemeToggle';
import { THEME_STORAGE_KEY } from '../utils/theme';

describe('ThemeToggle', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.setAttribute('data-theme', 'light');
  });
  afterEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
  });

  it('follows the theme already applied and does not store it on mount', () => {
    render(<ThemeToggle />);
    expect(screen.getByRole('button', { name: 'Switch to dark mode' })).toBeInTheDocument();
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it('does not store a theme when a controlled value is passed', () => {
    render(<ThemeToggle theme="dark" onToggle={() => {}} />);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it('stores the new choice when the user toggles', () => {
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole('button', { name: 'Switch to dark mode' }));
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
  });
});
