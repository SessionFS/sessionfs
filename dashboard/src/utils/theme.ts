import { getItem as lsGet, setItem as lsSet, removeItem as lsRemove } from './storage';

export type Theme = 'light' | 'dark';
export type ThemeChoice = Theme | 'system';

/** localStorage key shared with every theme reader/writer. */
export const THEME_STORAGE_KEY = 'sfs-theme';

/** The user's explicit choice, or null when following the system. */
export function getStoredTheme(): Theme | null {
  const stored = lsGet(THEME_STORAGE_KEY);
  return stored === 'light' || stored === 'dark' ? stored : null;
}

/** The OS colour-scheme preference ('dark' when it cannot be determined). */
export function getSystemTheme(): Theme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'dark';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * Effective theme: stored choice, else the data-theme attribute already on
 * <html>, else the system preference.
 */
export function getTheme(): Theme {
  const stored = getStoredTheme();
  if (stored) return stored;
  if (typeof document !== 'undefined') {
    const dom = document.documentElement.getAttribute('data-theme');
    if (dom === 'light' || dom === 'dark') return dom;
  }
  return getSystemTheme();
}

/** Write the resolved theme to <html> without touching storage. */
export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme);
}

/**
 * Apply and persist a choice. 'system' clears the stored value and follows
 * the OS preference.
 */
export function setTheme(choice: ThemeChoice): Theme {
  let resolved: Theme;
  if (choice === 'system') {
    resolved = getSystemTheme();
    lsRemove(THEME_STORAGE_KEY);
  } else {
    resolved = choice;
    lsSet(THEME_STORAGE_KEY, choice);
  }
  applyTheme(resolved);
  return resolved;
}
