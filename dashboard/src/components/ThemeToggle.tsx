import { useState } from 'react';
import { getTheme, setTheme as persistTheme } from '../utils/theme';
import { Tooltip } from './ui';
import { Icon } from './ui/Icon';
import { Moon, Sun } from 'lucide-react';

// No stored preference: follow what is already on <html> (set from the
// system preference at startup).
function getInitialTheme(): 'light' | 'dark' {
  return getTheme();
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
    // Persist only when the user toggles. Writing on mount or on an OS
    // change would turn the system preference into a stored choice and
    // undo "System" in Settings.
    persistTheme(theme === 'dark' ? 'light' : 'dark');
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


  return (
    <Tooltip content={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} describe={false} side="bottom">
      <button
        onClick={toggle}
        aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
        className="flex items-center justify-center w-8 h-8 rounded-md text-text-secondary hover:text-text-primary hover:bg-surface-hover transition-colors"
      >
        {theme === 'dark' ? (
          /* Sun icon */
          <Icon icon={Sun} />
        ) : (
          /* Moon icon */
          <Icon icon={Moon} />
        )}
      </button>
    </Tooltip>
  );
}
