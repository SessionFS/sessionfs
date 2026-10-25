import { useState } from 'react';
import { getStoredTheme, setTheme as applyThemeChoice, type ThemeChoice } from '../utils/theme';
import { Card, Select } from '../components/ui';
import { useNotificationSettings, useUpdateNotificationSettings } from '../hooks/useTicketNotifications';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';

function getInitialTheme(): ThemeChoice {
  return getStoredTheme() ?? 'system';
}

function NotificationPreferencesCard() {
  const { data } = useNotificationSettings();
  const update = useUpdateNotificationSettings();
  const { addToast } = useToast();
  const value = data?.ticket_email ?? 'your_turn';

  function handleChange(next: string) {
    update.mutate(
      { ticket_email: next },
      {
        onSuccess: () => addToast('success', 'Notification preference saved'),
        onError: (err) =>
          addToast('error', `Failed to save preference: ${errorMessage(err)}`),
      },
    );
  }

  return (
    <Card level="elevated" className="p-5">
      <h2 className="text-lg font-semibold text-text-primary mb-4">Notifications</h2>
      <div>
        <Select
          value={value}
          onValueChange={handleChange}
          options={[
            { value: 'off', label: 'Off' },
            { value: 'your_turn', label: 'Your turn only' },
            { value: 'all', label: 'All ticket updates' },
          ]}
          title="Ticket email"
        />
        <p className="text-xs text-text-tertiary opacity-60 mt-1.5">
          When a ticket you follow changes, SessionFS can email you a digest.
          “Your turn only” emails you only when an action is waiting on you.
        </p>
      </div>
    </Card>
  );
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

      <NotificationPreferencesCard />
    </div>
  );
}
