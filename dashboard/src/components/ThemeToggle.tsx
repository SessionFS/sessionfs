import { useEffect, useState } from 'react';
import { getItem as lsGet, setItem as lsSet } from '../utils/storage';

function getInitialTheme(): 'light' | 'dark' {
  const stored = lsGet('sfs-theme');
  if (stored === 'light' || stored === 'dark') return stored;
  // No stored preference — default to dark. System preference is ignored
  // because the dashboard ships dark-first; users toggle explicitly.
  return 'dark';
}

interface ThemeToggleProps {
  /** Controlled theme value. When set with onToggle, the component is fully controlled. */
  theme?: 'light' | 'dark';
  /** Called when the user toggles. When provided, the component is fully controlled. */
  onToggle?: () => void;
}

export default function ThemeToggle({ theme: controlledTheme, onToggle }: ThemeToggleProps) {
  const [internalTheme, setInternalTheme] = useState<'light' | 'dark'>(getInitialTheme);

  const theme = controlledTheme ?? internalTheme;

  const toggle = () => {
    document.documentElement.classList.add('theme-switching');
    if (onToggle) {
      onToggle();
    } else {
      setInternalTheme((t) => (t === 'dark' ? 'light' : 'dark'));
    }
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        document.documentElement.classList.remove('theme-switching');
      });
    });
  };

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    lsSet('sfs-theme', theme);
  }, [theme]);

  return (
    <button
      onClick={toggle}
      aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
      className="flex items-center justify-center w-8 h-8 rounded-[var(--radius-md)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-hover)] transition-colors"
    >
      {theme === 'dark' ? (
        /* Sun icon */
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="5" />
          <line x1="12" y1="1" x2="12" y2="3" />
          <line x1="12" y1="21" x2="12" y2="23" />
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
          <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
          <line x1="1" y1="12" x2="3" y2="12" />
          <line x1="21" y1="12" x2="23" y2="12" />
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
          <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
        </svg>
      ) : (
        /* Moon icon */
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}
