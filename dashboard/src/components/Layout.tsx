import { useState, useEffect } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useHandoffInbox } from '../hooks/useHandoffs';
import { useMe } from '../hooks/useMe';
import { useTransfers } from '../transfers/useTransfers';
import { useMyInvites } from '../invites/useInvites';
import SearchBar from './SearchBar';
import ThemeToggle from './ThemeToggle';
import { Badge } from './Badge';
import { Dropdown } from './ui';
import Wordmark from './Wordmark';

const NAV_LINKS = [
  { to: '/', label: 'Sessions', match: (p: string) => p === '/' },
  { to: '/projects', label: 'Projects', match: (p: string) => p.startsWith('/projects') },
  { to: '/handoffs', label: 'Handoffs', match: (p: string) => p.startsWith('/handoffs') },
  { to: '/transfers', label: 'Transfers', match: (p: string) => p.startsWith('/transfers') },
  // v0.10.22 — pending OrgInvites for this user (matched on email).
  // Renders the badge from useMyInvites; the nav row stays visible
  // even when zero so users can find the page if an admin later
  // sends an invite that arrives while they're already logged in.
  { to: '/invites', label: 'Invites', match: (p: string) => p.startsWith('/invites') },
  // Settings catches /settings AND any /settings/* EXCEPT /settings/billing
  // and /settings/organization which have their own nav entries.
  {
    to: '/settings',
    label: 'Settings',
    match: (p: string) =>
      p.startsWith('/settings') && p !== '/settings/billing' && p !== '/settings/organization',
  },
  {
    to: '/settings/organization',
    label: 'Organization',
    match: (p: string) => p === '/settings/organization',
    // Visible only when the user has a default org. Free-tier solo users
    // don't see Organization in the nav at all.
    orgOnly: true,
  },
  { to: '/settings/billing', label: 'Billing', match: (p: string) => p === '/settings/billing' },
];

function siteHref(path: string): string {
  const theme = typeof document !== 'undefined'
    ? document.documentElement.getAttribute('data-theme') || 'dark'
    : 'dark';
  const sep = path.includes('?') ? '&' : '?';
  return `https://sessionfs.dev${path}${sep}theme=${theme}`;
}

export default function Layout() {
  const { logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const inbox = useHandoffInbox();
  const me = useMe();
  const isAdmin = me.data?.tier === 'admin';
  const hasOrg = !!me.data?.default_org_id;
  const pendingCount = inbox.data?.handoffs.filter((h) => h.status === 'pending').length ?? 0;
  const incomingTransfers = useTransfers('incoming', 'pending');
  const transfersPendingCount = incomingTransfers.data?.transfers.length ?? 0;
  const myInvites = useMyInvites();
  const invitesPendingCount = myInvites.data?.invites.length ?? 0;

  const [drawerOpen, setDrawerOpen] = useState(false);

  // Own the theme state so the header toggle and the account menu stay in sync.
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const stored = (() => { try { return localStorage.getItem('sfs-theme'); } catch { return null; } })();
    return stored === 'light' || stored === 'dark' ? stored : 'dark';
  });

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'));

  // Close drawer on route change
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  // Lock body scroll when drawer is open
  useEffect(() => {
    if (drawerOpen) {
      document.body.style.overflow = 'hidden';
      return () => { document.body.style.overflow = ''; };
    }
  }, [drawerOpen]);

  const allNavLinks = [
    ...NAV_LINKS.filter((link) => !link.orgOnly || !!me.data?.default_org_id),
    ...(isAdmin ? [{ to: '/admin', label: 'Admin', match: (p: string) => p === '/admin' }] : []),
    { to: '/help', label: 'Help', match: (p: string) => p === '/help' },
  ];

  const tierLabel = me.data?.tier || 'free';
  const tierVariant = tierLabel === 'admin' ? 'info' : tierLabel === 'team' ? 'success' : 'default';
  const userInitial = (me.data?.email?.[0] || 'U').toUpperCase();

  // ── Account menu icon SVGs ──
  const iconProps = { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };

  const orgIcon = (
    <svg {...iconProps}><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>
  );
  const invitesIcon = (
    <svg {...iconProps}><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><line x1="19" y1="8" x2="19" y2="14" /><line x1="22" y1="11" x2="16" y2="11" /></svg>
  );
  const billingIcon = (
    <svg {...iconProps}><rect x="2" y="5" width="20" height="14" rx="2" /><line x1="2" y1="10" x2="22" y2="10" /></svg>
  );
  const settingsIcon = (
    <svg {...iconProps}><circle cx="12" cy="12" r="3" /><path d="M12 1v2m0 18v2M4.22 4.22l1.42 1.42m12.72 12.72 1.42 1.42M1 12h2m18 0h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" /></svg>
  );
  const helpIcon = (
    <svg {...iconProps}><circle cx="12" cy="12" r="9" /><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7" /><line x1="12" y1="17" x2="12" y2="17.01" /></svg>
  );
  const themeIcon = (
    <svg {...iconProps}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
  );
  const adminIcon = (
    <svg {...iconProps}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>
  );
  const logoutIcon = (
    <svg {...iconProps}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" /></svg>
  );

  // ── Account menu items ──
  const tierBadgeEl = (
    <Badge variant={tierVariant as 'default' | 'success' | 'info'} label={tierLabel} size="sm" />
  );

  const accountMenuItems: Array<{ key: string; label: string; icon?: React.ReactNode; danger?: boolean; disabled?: boolean; separator?: boolean; header?: boolean; badge?: string }> = [
    // Section 1 — Identity header (non-interactive)
    { key: 'identity', label: me.data?.email || 'Unknown', icon: tierBadgeEl, header: true },
    // Section 2 — Organization (only when user has a default org)
    ...(hasOrg
      ? [
          { key: 'org-sep', label: '', separator: true as const },
          { key: 'organization', label: 'Organization', icon: orgIcon },
          {
            key: 'invites',
            label: 'Invites',
            icon: invitesIcon,
            badge: invitesPendingCount > 0 ? String(invitesPendingCount) : undefined,
          },
          { key: 'billing', label: 'Billing', icon: billingIcon },
          { key: 'personal-sep', label: '', separator: true as const },
        ]
      : []),
    // Section 3 — Personal
    { key: 'settings', label: 'Settings', icon: settingsIcon },
    { key: 'help', label: 'Help', icon: helpIcon },
    {
      key: 'theme',
      label: `Theme: ${theme === 'dark' ? 'Dark' : 'Light'}`,
      icon: themeIcon,
    },
    ...(isAdmin
      ? [{ key: 'admin', label: 'Admin', icon: adminIcon }]
      : []),
    // Section 4 — Logout
    { key: 'logout-sep', label: '', separator: true as const },
    { key: 'logout', label: 'Logout', icon: logoutIcon, danger: true },
  ];

  function handleAccountMenuSelect(key: string) {
    switch (key) {
      case 'organization':
        navigate('/settings/organization');
        break;
      case 'invites':
        navigate('/invites');
        break;
      case 'billing':
        navigate('/settings/billing');
        break;
      case 'settings':
        navigate('/settings');
        break;
      case 'help':
        navigate('/help');
        break;
      case 'admin':
        navigate('/admin');
        break;
      case 'theme':
        toggleTheme();
        break;
      case 'logout':
        logout();
        break;
    }
  }

  return (
    <div className="flex flex-col min-h-screen">
      <header
        className="relative flex items-center justify-between px-5 overflow-hidden"
        style={{
          height: 56,
          borderBottom: '1px solid var(--border)',
          backgroundColor: 'var(--bg-secondary)',
        }}
      >
        <div className="shell-divider pointer-events-none absolute inset-x-10 top-0 h-px" />
        {/* Left: Hamburger (mobile) + Logo */}
        <div className="flex items-center gap-2 shrink-0">
          <button
            className="md:hidden flex items-center justify-center w-8 h-8 rounded-[var(--radius-md)] transition-colors hover:bg-[var(--surface-hover)]"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation menu"
          >
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <line x1="3" y1="5" x2="17" y2="5" />
              <line x1="3" y1="10" x2="17" y2="10" />
              <line x1="3" y1="15" x2="17" y2="15" />
            </svg>
          </button>
          <Link
            to="/"
            className="flex items-center hover:opacity-90 transition-opacity"
            aria-label="SessionFS home"
          >
            <Wordmark size="sm" showTagline />
          </Link>
        </div>

        {/* Center: Nav links (desktop only) */}
        <nav className="hidden md:flex items-center gap-1 overflow-x-auto whitespace-nowrap">
          {NAV_LINKS.filter((link) => !link.orgOnly || !!me.data?.default_org_id).map(({ to, label, match }) => {
            const active = match(location.pathname);
            return (
              <Link
                key={to}
                to={to}
                className={`px-3 py-1.5 rounded-[var(--radius-md)] text-[13px] font-medium transition-colors ${
                  active
                    ? 'bg-[var(--surface)] border border-[var(--border)] text-[var(--text-primary)]'
                    : 'border border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-hover)]'
                }`}
              >
                {label}
                {label === 'Handoffs' && pendingCount > 0 && (
                  <span
                    className="ml-1 px-1.5 py-0.5 text-xs rounded-full font-medium"
                    style={{
                      backgroundColor: 'rgba(240,192,64,0.15)',
                      color: 'var(--warning)',
                    }}
                    role="status"
                    aria-label={`${pendingCount} pending handoff${pendingCount === 1 ? '' : 's'}`}
                  >
                    {pendingCount}
                  </span>
                )}
                {label === 'Transfers' && transfersPendingCount > 0 && (
                  <span
                    className="ml-1 px-1.5 py-0.5 text-xs rounded-full font-medium"
                    style={{
                      backgroundColor: 'rgba(240,192,64,0.15)',
                      color: 'var(--warning)',
                    }}
                    role="status"
                    aria-label={`${transfersPendingCount} pending transfer${transfersPendingCount === 1 ? '' : 's'}`}
                  >
                    {transfersPendingCount}
                  </span>
                )}
                {label === 'Invites' && invitesPendingCount > 0 && (
                  <span
                    className="ml-1 px-1.5 py-0.5 text-xs rounded-full font-medium"
                    style={{
                      backgroundColor: 'rgba(240,192,64,0.15)',
                      color: 'var(--warning)',
                    }}
                    role="status"
                    aria-label={`${invitesPendingCount} pending invite${invitesPendingCount === 1 ? '' : 's'}`}
                  >
                    {invitesPendingCount}
                  </span>
                )}
              </Link>
            );
          })}
          {isAdmin && (
            <Link
              to="/admin"
              className={`px-3 py-1.5 rounded-[var(--radius-md)] text-[13px] font-medium transition-colors ${
                location.pathname === '/admin'
                  ? 'bg-[var(--surface)] border border-[var(--border)] text-[var(--text-primary)]'
                  : 'border border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-hover)]'
              }`}
            >
              Admin
            </Link>
          )}
          <SearchBar />
        </nav>

        {/* Right: ThemeToggle + Account menu */}
        <div className="flex items-center gap-2 shrink-0">
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <Dropdown
            trigger={
              <button
                className="flex items-center gap-2 rounded-[var(--radius-md)] px-2 py-1 transition-colors hover:bg-[var(--surface-hover)]"
                aria-haspopup="menu"
                aria-expanded={undefined}
              >
                <span
                  className="flex items-center justify-center w-7 h-7 rounded-full text-xs font-semibold"
                  style={{
                    backgroundColor: 'var(--brand)',
                    color: 'var(--text-inverse)',
                  }}
                >
                  {userInitial}
                </span>
                <Badge variant={tierVariant as 'default' | 'success' | 'info'} label={tierLabel} size="sm" />
              </button>
            }
            items={accountMenuItems}
            onSelect={handleAccountMenuSelect}
            menuLabel="Account menu"
            minWidthClass="min-w-[240px]"
          />
        </div>
      </header>

      {/* Mobile navigation drawer */}
      <div
        className={`fixed inset-0 z-50 md:hidden ${drawerOpen ? '' : 'pointer-events-none'}`}
        role="dialog"
        aria-modal="true"
        aria-label="Navigation menu"
      >
        {/* Backdrop */}
        <div
          className={`absolute inset-0 bg-black transition-opacity duration-300 ${
            drawerOpen ? 'opacity-50' : 'opacity-0'
          }`}
          onClick={() => setDrawerOpen(false)}
        />
        {/* Drawer panel */}
        <nav
          className={`absolute top-0 left-0 bottom-0 w-64 flex flex-col transition-transform duration-300 ease-in-out ${
            drawerOpen ? 'translate-x-0' : '-translate-x-full'
          }`}
          style={{
            backgroundColor: 'var(--bg-secondary)',
            borderRight: '1px solid var(--border)',
          }}
        >
          {/* Drawer header */}
          <div className="flex items-center justify-between px-4" style={{ height: 56 }}>
            <Wordmark size="sm" />
            <button
              onClick={() => setDrawerOpen(false)}
              className="flex items-center justify-center w-8 h-8 rounded-[var(--radius-md)] transition-colors hover:bg-[var(--surface-hover)]"
              aria-label="Close navigation menu"
            >
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <line x1="4" y1="4" x2="14" y2="14" />
                <line x1="14" y1="4" x2="4" y2="14" />
              </svg>
            </button>
          </div>
          <div style={{ borderTop: '1px solid var(--border)' }} />
          {/* Drawer links */}
          <div className="flex flex-col py-2 px-2 gap-0.5">
            {allNavLinks.map(({ to, label, match }) => {
              const active = match(location.pathname);
              return (
                <Link
                  key={to}
                  to={to}
                  onClick={() => setDrawerOpen(false)}
                  className={`flex items-center gap-2 px-3 py-2.5 rounded-[var(--radius-md)] text-[14px] transition-colors ${
                    active
                      ? 'bg-[var(--surface-hover)] text-[var(--brand)] font-medium'
                      : 'text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)]'
                  }`}
                >
                  {label}
                  {label === 'Handoffs' && pendingCount > 0 && (
                    <span
                      className="px-1.5 py-0.5 text-xs rounded-full font-medium"
                      style={{
                        backgroundColor: 'rgba(240,192,64,0.15)',
                        color: 'var(--warning)',
                      }}
                      role="status"
                      aria-label={`${pendingCount} pending handoff${pendingCount === 1 ? '' : 's'}`}
                    >
                      {pendingCount}
                    </span>
                  )}
                  {label === 'Transfers' && transfersPendingCount > 0 && (
                    <span
                      className="px-1.5 py-0.5 text-xs rounded-full font-medium"
                      style={{
                        backgroundColor: 'rgba(240,192,64,0.15)',
                        color: 'var(--warning)',
                      }}
                      role="status"
                      aria-label={`${transfersPendingCount} pending transfer${transfersPendingCount === 1 ? '' : 's'}`}
                    >
                      {transfersPendingCount}
                    </span>
                  )}
                  {label === 'Invites' && invitesPendingCount > 0 && (
                    <span
                      className="px-1.5 py-0.5 text-xs rounded-full font-medium"
                      style={{
                        backgroundColor: 'rgba(240,192,64,0.15)',
                        color: 'var(--warning)',
                      }}
                      role="status"
                      aria-label={`${invitesPendingCount} pending invite${invitesPendingCount === 1 ? '' : 's'}`}
                    >
                      {invitesPendingCount}
                    </span>
                  )}
                </Link>
              );
            })}
          </div>
        </nav>
      </div>
      <main className="relative flex-1 bg-[var(--bg-primary)] overflow-x-clip">
        <div
          className="pointer-events-none absolute inset-x-0 top-0 h-48 opacity-80"
          style={{
            background: 'radial-gradient(60rem 24rem at 18% -8%, color-mix(in srgb, var(--brand) 12%, transparent), transparent 72%)',
          }}
        />
        <div
          className="pointer-events-none absolute right-0 top-0 h-56 w-96 opacity-70"
          style={{
            background: 'radial-gradient(22rem 18rem at 85% 0%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 76%)',
          }}
        />
        <div key={location.pathname} className="page-enter relative flex flex-col flex-1 min-h-0">
          <Outlet />
        </div>
      </main>
      <footer className="text-center py-8 text-[11px] text-[var(--text-tertiary)] border-t border-[var(--border)]">
        <span className="font-semibold text-[var(--text-secondary)]">SessionFS</span>
        <span className="mx-2 text-[var(--text-tertiary)]">·</span>
        Memory layer for AI coding agents
        <span className="mx-2 text-[var(--text-tertiary)]">·</span>
        v{__APP_VERSION__}
        <span className="mx-2 text-[var(--text-tertiary)]">·</span>
        <a href={siteHref('/quickstart/')} className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Docs</a> &middot;{' '}
        <a href={siteHref('/')} className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Status</a> &middot;{' '}
        <a href="mailto:support@sessionfs.dev" className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Support</a>
      </footer>
    </div>
  );
}
