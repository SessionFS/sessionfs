/**
 * GitHub #92 — the Account page shows the org personas that represent the
 * caller, each linking to the org personas tab for editing.
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import AccountSection from './AccountSection';

// "Your groups" fetches through react-query; it has its own suite.
vi.mock('./YourGroupsCard', () => ({ default: () => null }));

function profile(extra: Record<string, unknown> = {}) {
  return {
    email: 'kelvin@example.com',
    email_verified: true,
    tier: 'pro',
    effective_tier: 'team',
    capture_health: null,
    last_client_version: null,
    represented_personas: [
      {
        persona_id: 'p_1',
        name: 'gcp-infra',
        role: 'Infra steward',
        org_id: 'org_1',
        org_name: 'Acme',
      },
    ],
    ...extra,
  };
}

function renderAt(profileData: Record<string, unknown>) {
  return render(
    <MemoryRouter>
      <AccountSection profile={profile(profileData)} logout={vi.fn()} />
    </MemoryRouter>,
  );
}

describe('AccountSection represented personas', () => {
  it('shows a badge linking to that persona\'s org personas tab', () => {
    renderAt({});
    const link = screen.getByRole('link', { name: /gcp-infra/i });
    expect(link).toHaveAttribute('href', '/settings/organization?tab=personas&org=org_1');
  });

  it('deep-links each badge to its own org (secondary orgs included)', () => {
    renderAt({
      represented_personas: [
        {
          persona_id: 'p_1',
          name: 'gcp-infra',
          role: 'Infra steward',
          org_id: 'org_1',
          org_name: 'Acme',
        },
        {
          persona_id: 'p_2',
          name: 'release-shepherd',
          role: 'Release owner',
          org_id: 'org_2',
          org_name: 'Globex',
        },
      ],
    });
    expect(screen.getByRole('link', { name: /gcp-infra/i })).toHaveAttribute(
      'href',
      '/settings/organization?tab=personas&org=org_1',
    );
    expect(screen.getByRole('link', { name: /release-shepherd/i })).toHaveAttribute(
      'href',
      '/settings/organization?tab=personas&org=org_2',
    );
  });

  it('renders nothing when the user represents no persona', () => {
    renderAt({ represented_personas: [] });
    expect(screen.queryByText(/Represented personas/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /gcp-infra/i })).not.toBeInTheDocument();
  });

  it('renders nothing when the field is absent (older server)', () => {
    renderAt({ represented_personas: undefined });
    expect(screen.queryByText(/Represented personas/i)).not.toBeInTheDocument();
  });
});
