import { NavLink, Navigate, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { PageContainer, PageHeader, Select } from '../components/ui';
import AccountSection from './AccountSection';
import PreferencesSection from './PreferencesSection';
import IntegrationsSection from './IntegrationsSection';
import DataProtectionSection from './DataProtectionSection';
import SyncSection from './SyncSection';
import AssistantSection from './AssistantSection';

type SettingsSectionKey =
  | 'account'
  | 'preferences'
  | 'integrations'
  | 'data-protection'
  | 'assistant'
  | 'sync';

const SETTINGS_SECTIONS: { key: SettingsSectionKey; label: string }[] = [
  { key: 'account', label: 'Account' },
  { key: 'preferences', label: 'Preferences' },
  { key: 'integrations', label: 'Integrations' },
  { key: 'data-protection', label: 'Data protection' },
  { key: 'assistant', label: 'Assistant' },
  { key: 'sync', label: 'Sync' },
];

const DEFAULT_SECTION: SettingsSectionKey = 'account';

function isSection(value: string | undefined): value is SettingsSectionKey {
  return SETTINGS_SECTIONS.some((s) => s.key === value);
}

/**
 * Settings shell: left sub-nav (a select on small screens) and one section
 * per route, /settings/:section. Billing and Organization are separate pages
 * with their own static routes, which outrank this dynamic one.
 */
export default function SettingsPage() {
  const { auth, logout } = useAuth();
  const { section } = useParams();
  const { search } = useLocation();
  const navigate = useNavigate();
  const { data: profile } = useQuery({
    queryKey: ['me'],
    queryFn: () => auth!.client.getMe(),
    enabled: !!auth,
  });

  if (!auth) return null;

  // The GitHub link flow returns to /settings?github=<outcome>; its notices
  // and the installation chooser live in Integrations. Query params travel
  // with the redirect.
  const hasGitHubReturn = new URLSearchParams(search).has('github');
  if (hasGitHubReturn && section !== 'integrations') {
    return <Navigate to={`/settings/integrations${search}`} replace />;
  }
  if (!isSection(section)) {
    return <Navigate to={`/settings/${DEFAULT_SECTION}${search}`} replace />;
  }

  return (
    <PageContainer>
      <PageHeader title="Settings" />

      <div className="md:flex md:gap-8">
        <div className="md:hidden mb-4">
          <Select
            value={section}
            onValueChange={(v) => navigate(`/settings/${v}`)}
            options={SETTINGS_SECTIONS.map((s) => ({ value: s.key, label: s.label }))}
            aria-label="Settings section"
          />
        </div>

        <nav aria-label="Settings sections" className="hidden md:block w-44 shrink-0">
          <ul className="space-y-1">
            {SETTINGS_SECTIONS.map((s) => (
              <li key={s.key}>
                <NavLink
                  to={`/settings/${s.key}`}
                  className={({ isActive }) =>
                    `block rounded-md border px-3 py-1.5 text-sm font-medium transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
                      isActive
                        ? 'bg-surface border-border text-text-primary'
                        : 'border-transparent text-text-secondary hover:text-text-primary hover:bg-surface-hover'
                    }`
                  }
                >
                  {s.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-w-0 flex-1 max-w-2xl">
          {section === 'account' && <AccountSection profile={profile} logout={logout} />}
          {section === 'preferences' && <PreferencesSection />}
          {section === 'integrations' && <IntegrationsSection />}
          {section === 'data-protection' && <DataProtectionSection />}
          {section === 'assistant' && <AssistantSection />}
          {section === 'sync' && <SyncSection />}
        </div>
      </div>
    </PageContainer>
  );
}
