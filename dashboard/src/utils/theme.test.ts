import { describe, expect, it } from 'vitest';
import { getStoredTheme, getTheme, setTheme } from './theme';

describe('theme util', () => {
  it('persists an explicit choice and applies it to <html>', () => {
    expect(setTheme('light')).toBe('light');
    expect(window.localStorage.getItem('sfs-theme')).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(getStoredTheme()).toBe('light');
    expect(getTheme()).toBe('light');
  });

  it('clears the stored value when following the system', () => {
    setTheme('dark');
    setTheme('system');
    expect(window.localStorage.getItem('sfs-theme')).toBeNull();
    expect(getStoredTheme()).toBeNull();
    expect(['light', 'dark']).toContain(document.documentElement.getAttribute('data-theme'));
  });

  it('falls back to the data-theme attribute when nothing is stored', () => {
    document.documentElement.setAttribute('data-theme', 'light');
    expect(getTheme()).toBe('light');
  });
});
