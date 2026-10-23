import { useState } from 'react';
import { getStoredTheme, setTheme as applyThemeChoice, type ThemeChoice } from '../utils/theme';
import { Card, Select } from '../components/ui';

function getInitialTheme(): ThemeChoice {
  return getStoredTheme() ?? 'system';
}

export default function PreferencesSection() {
  const [theme, setTheme] = useState<ThemeChoice>(getInitialTheme);

  function handleThemeChange(value: string) {
    const choice = value as ThemeChoice;
    setTheme(choice);
    applyThemeChoice(choice);
  }

  return (
    <div className="space-y-5">
      <Card level="elevated" className="p-5">
        <h2 className="text-lg font-semibold text-text-primary mb-4">Appearance</h2>
        <div>
          <Select
            value={theme}
            onValueChange={(v) => handleThemeChange(v)}
            options={[
              { value: 'light', label: 'Light' },
              { value: 'dark', label: 'Dark' },
              { value: 'system', label: 'System' },
            ]}
            title="Theme"
          />
          <p className="text-xs text-text-tertiary opacity-60 mt-1.5">
            Choose how SessionFS looks. System follows your OS preference.
          </p>
        </div>
      </Card>
    </div>
  );
}
