import { useState, useEffect, type ReactNode } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useHandoffInbox } from '../hooks/useHandoffs';
import { useMe } from '../hooks/useMe';
import { useTransfers } from '../transfers/useTransfers';
import { useMyInvites } from '../invites/useInvites';
import SearchBar from './SearchBar';
import ThemeToggle from './ThemeToggle';
import { Badge } from './Badge';
import { Dropdown, Tooltip } from './ui';
import Wordmark from './Wordmark';

// ── Icon factory ──
const iconProps = {
  width: 16, height: 16, viewBox: '0 0 24 24',
  fill: 'none', stroke: 'currentColor', strokeWidth: 1.5,
  strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
};

function mkIcon(d: ReactNode) {
  return <svg {...iconProps}>{d}</svg>;
}

// ── Sidebar nav item icons ──
const sessionsIcon = mkIcon(
  <><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></>
);
const projectsIcon = mkIcon(
  <><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></>
);
const handoffsIcon = mkIcon(
  <><path d="M16 3h5v5M21 3l-7 7M8 21H3v-5M3 21l7-7" /><circle cx="12" cy="12" r="2" /></>
);
const transfersIcon = mkIcon(
  <><polyline points="17 1 21 5 17 9" /><path d="M3 11V9a4 4 0 0 1 4-4h14" /><polyline points="7 23 3 19 7 15" /><path d="M21 13v2a4 4 0 0 1-4 4H3" /></>
);
const invitesIcon = mkIcon(
  <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><line x1="19" y1="8" x2="19" y2="14" /><line x1="22" y1="11" x2="16" y2="11" /></>
);
const orgIcon = mkIcon(
  <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>
);
const billingIcon = mkIcon(
  <><rect x="2" y="5" width="20" height="14" rx="2" /><line x1="2" y1="10" x2="22" y2="10" /></>
);
const settingsIcon = mkIcon(
  <><circle cx="12" cy="12" r="3" /><path d="M12 1v2m0 18v2M4.22 4.22l1.42 1.42m12.72 12.72 1.42 1.42M1 12h2m18 0h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" /></>
);
const helpIcon = mkIcon(
  <><circle cx="12" cy="12" r="9" /><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7" /><line x1="12" y1="17" x2="12" y2="17.01" /></>
);
const adminIcon = mkIcon(
  <><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></>
);
const collapseIcon = mkIcon(
  <><polyline points="11 17 6 12 11 7" /><polyline points="18 17 13 12 18 7" /></>
);
const expandIcon = mkIcon(
  <><polyline points="13 17 18 12 13 7" /><polyline points="6 17 11 12 6 7" /></>
);

// ── Nav config ──
interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  match: (p: string) => boolean;
  orgOnly?: boolean;
  adminOnly?: boolean;
  badgeKey?: 'pending' | 'transfers' | 'invites';
}

interface NavGroup {
  label?: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    items: [
      { to: '/', label: 'Sessions', icon: sessionsIcon, match: (p: string) => p === '/' },
      { to: '/projects', label: 'Projects', icon: projectsIcon, match: (p: string) => p.startsWith('/projects') },
      { to: '/handoffs', label: 'Handoffs', icon: handoffsIcon, match: (p: string) => p.startsWith('/handoffs'), badgeKey: 'pending' },
    ],
  },
  {
    label: 'Organization',
    items: [
      { to: '/transfers', label: 'Transfers', icon: transfersIcon, match: (p: string) => p.startsWith('/transfers'), badgeKey: 'transfers' },
      { to: '/invites', label: 'Invites', icon: invitesIcon, match: (p: string) => p.startsWith('/invites'), badgeKey: 'invites' },
      { to: '/settings/organization', label: 'Organization', icon: orgIcon, match: (p: string) => p === '/settings/organization', orgOnly: true },
      { to: '/settings/billing', label: 'Billing', icon: billingIcon, match: (p: string) => p === '/settings/billing' },
    ],
  },
];

const BOTTOM_ITEMS: NavItem[] = [
  {
    to: '/settings',
    label: 'Settings',
    icon: settingsIcon,
    match: (p: string) => p.startsWith('/settings') && p !== '/settings/billing' && p !== '/settings/organization',
  },
  { to: '/help', label: 'Help', icon: helpIcon, match: (p: string) => p === '/help' },
  { to: '/admin', label: 'Admin', icon: adminIcon, match: (p: string) => p === '/admin', adminOnly: true },
];

function siteHref(path: string): string {
  const theme = typeof document !== 'undefined'
    ? document.documentElement.getAttribute('data-theme') || 'dark'
    : 'dark';
  const sep = path.includes('?') ? '&' : '?';
  return `https://sessionfs.dev${path}${sep}theme=${theme}`;
}

// ── Sidebar nav link ──
function SidebarLink({
  item,
  active,
  collapsed,
  badgeCount,
  onClick,
}: {
  item: NavItem;
  active: boolean;
  collapsed: boolean;
  badgeCount?: number;
  onClick?: () => void;
}) {
  const link = (
    <Link
      to={item.to}
      onClick={onClick}
      className={`flex items-center gap-2.5 rounded-md text-sm font-medium transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
        collapsed ? 'justify-center w-9 h-9 mx-auto' : 'px-3 py-1.5'
      } ${
        active
          ? 'bg-surface border border-border text-text-primary'
          : 'border border-transparent text-text-secondary hover:text-text-primary hover:bg-surface-hover'
      }`}
      aria-current={active ? 'page' : undefined}
    >
      <span className="shrink-0 flex items-center justify-center w-4 h-4">{item.icon}</span>
      {!collapsed && (
        <span className="truncate">{item.label}</span>
      )}
      {!collapsed && badgeCount != null && badgeCount > 0 && (
        <span
          className="ml-auto px-1.5 py-0.5 text-xs rounded-full font-medium tabular-nums"
          style={{ backgroundColor: 'rgba(240,192,64,0.15)', color: 'var(--warning)' }}
          role="status"
          aria-label={`${badgeCount} pending`}
        >
          {badgeCount}
        </span>
      )}
    </Link>
  );

  if (collapsed) {
    return <Tooltip content={item.label}>{link}</Tooltip>;
  }
  return link;
}

// ── Layout ──
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

  // Sidebar collapse — persisted to localStorage
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('sfs-sidebar-collapsed') === 'true'; } catch { return false; }
  });

  function toggleCollapsed() {
    setCollapsed((prev) => {
      const next = !prev;
      try { localStorage.setItem('sfs-sidebar-collapsed', String(next)); } catch { /* noop */ }
      return next;
    });
  }

  // Theme
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const stored = (() => { try { return localStorage.getItem('sfs-theme'); } catch { return null; } })();
    return stored === 'light' || stored === 'dark' ? stored : 'dark';
  });

  const toggleTheme = () => {
    document.documentElement.classList.add('theme-switching');
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'));
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        document.documentElement.classList.remove('theme-switching');
      });
    });
  };

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

  // ── Badge counts per key ──
  function badgeCount(key?: string): number | undefined {
    switch (key) {
      case 'pending': return pendingCount;
      case 'transfers': return transfersPendingCount;
      case 'invites': return invitesPendingCount;
      default: return undefined;
    }
  }

  // ── Filter nav items ──
  function isItemVisible(item: NavItem): boolean {
    if (item.orgOnly && !hasOrg) return false;
    if (item.adminOnly && !isAdmin) return false;
    return true;
  }

  const filteredGroups = NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter(isItemVisible),
  })).filter((g) => g.items.length > 0);

  const filteredBottom = BOTTOM_ITEMS.filter(isItemVisible);

  // ── Account menu items ──
  const tierLabel = me.data?.tier || 'free';
  const tierVariant = tierLabel === 'admin' ? 'info' : tierLabel === 'team' ? 'success' : 'default';
  const userInitial = (me.data?.email?.[0] || 'U').toUpperCase();

  const tierBadgeEl = (
    <Badge variant={tierVariant as 'default' | 'success' | 'info'} label={tierLabel} size="sm" />
  );

  const logoutIconEl = mkIcon(
    <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" /></>
  );
  const themeIconEl = mkIcon(
    <><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></>
  );

  const accountMenuItems: Array<{ key: string; label: string; icon?: ReactNode; danger?: boolean; disabled?: boolean; separator?: boolean; header?: boolean; badge?: string }> = [
    { key: 'identity', label: me.data?.email || 'Unknown', icon: tierBadgeEl, header: true },
    ...(hasOrg
      ? [
          { key: 'org-sep', label: '', separator: true as const },
          { key: 'organization', label: 'Organization', icon: orgIcon },
          { key: 'invites', label: 'Invites', icon: invitesIcon, badge: invitesPendingCount > 0 ? String(invitesPendingCount) : undefined },
          { key: 'billing', label: 'Billing', icon: billingIcon },
          { key: 'personal-sep', label: '', separator: true as const },
        ]
      : []),
    { key: 'settings', label: 'Settings', icon: settingsIcon },
    { key: 'help', label: 'Help', icon: helpIcon },
    { key: 'theme', label: `Theme: ${theme === 'dark' ? 'Dark' : 'Light'}`, icon: themeIconEl },
    ...(isAdmin ? [{ key: 'admin', label: 'Admin', icon: adminIcon }] : []),
    { key: 'logout-sep', label: '', separator: true as const },
    { key: 'logout', label: 'Logout', icon: logoutIconEl, danger: true },
  ];

  function handleAccountMenuSelect(key: string) {
    switch (key) {
      case 'organization': navigate('/settings/organization'); break;
      case 'invites': navigate('/invites'); break;
      case 'billing': navigate('/settings/billing'); break;
      case 'settings': navigate('/settings'); break;
      case 'help': navigate('/help'); break;
      case 'admin': navigate('/admin'); break;
      case 'theme': toggleTheme(); break;
      case 'logout': logout(); break;
    }
  }

  // ── Render sidebar nav groups ──
  function renderNavItems(onAnyClick?: () => void) {
    return (
      <>
        {filteredGroups.map((group, gi) => (
          <div key={gi}>
            {group.label && !collapsed && (
              <div className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mt-5 mb-1 px-2">
                {group.label}
              </div>
            )}
            <div className={`flex flex-col ${collapsed ? 'items-center gap-0.5' : 'gap-0.5'}`}>
              {group.items.map((item) => (
                <SidebarLink
                  key={item.to}
                  item={item}
                  active={item.match(location.pathname)}
                  collapsed={collapsed}
                  badgeCount={badgeCount(item.badgeKey)}
                  onClick={onAnyClick}
                />
              ))}
            </div>
          </div>
        ))}
        {/* Spacer pushes bottom items down */}
        <div className="flex-1" />
        <div className={`flex flex-col ${collapsed ? 'items-center gap-0.5' : 'gap-0.5'}`}>
          {filteredBottom.map((item) => (
            <SidebarLink
              key={item.to}
              item={item}
              active={item.match(location.pathname)}
              collapsed={collapsed}
              onClick={onAnyClick}
            />
          ))}
        </div>
      </>
    );
  }

  return (
    <div className="flex">
      {/* ── Sidebar (desktop ≥md) ── */}
      <aside
        className="hidden md:flex flex-col shrink-0 h-screen sticky top-0"
        style={{
          width: collapsed ? 56 : 240,
          backgroundColor: 'var(--bg-secondary)',
          borderRight: '1px solid var(--border)',
          transition: 'width 150ms ease',
        }}
      >
        {/* Top section — non-scrollable */}
        {/* Brand */}
        <div className={`flex items-center ${collapsed ? 'justify-center' : 'px-4'} h-14`}>
          {collapsed ? (
            <Link to="/" aria-label="SessionFS home" className="flex items-center">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="var(--brand)" strokeWidth="2" strokeLinecap="round">
                <path d="M6,4 Q2,4 2,8 L2,16 Q2,20 6,20" />
                <path d="M18,4 Q22,4 22,8 L22,16 Q22,20 18,20" />
                <circle cx="12" cy="12" r="2.5" fill="var(--brand)" />
              </svg>
            </Link>
          ) : (
            <Link to="/" aria-label="SessionFS home" className="flex items-center hover:opacity-90 transition-opacity">
              <Wordmark size="sm" showTagline={false} />
            </Link>
          )}
        </div>

        {/* Org chip */}
        {hasOrg && !collapsed && (
          <div className="px-3 pb-2">
            <Link
              to="/settings/organization"
              className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-surface-hover transition-colors text-2xs text-text-tertiary font-mono truncate"
            >
              <span className="shrink-0 w-3.5 h-3.5">{orgIcon}</span>
              <span className="truncate">{me.data?.default_org_id}</span>
            </Link>
          </div>
        )}

        {/* SearchBar — the input IS the search trigger */}
        <div className="px-2 pb-2">
          <SearchBar inputId="sidebar-search-input" className="relative w-full" />
        </div>

        {/* Nav groups — scrollable middle section */}
        <nav aria-label="Primary" className="flex flex-col flex-1 px-2 overflow-y-auto">
          {renderNavItems()}
        </nav>

        {/* Collapse toggle — bottom pinned, non-scrollable */}
        <div className={`flex ${collapsed ? 'justify-center' : 'justify-end'} px-2 py-2`}>
          <button
            type="button"
            onClick={toggleCollapsed}
            className="flex items-center justify-center w-7 h-7 rounded-sm hover:bg-surface-hover transition-colors text-text-tertiary hover:text-text-secondary"
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? expandIcon : collapseIcon}
          </button>
        </div>
      </aside>

      {/* ── Content area ── */}
      <div className="flex-1 flex flex-col min-h-screen min-w-0">
        {/* ── Header (slim) ── */}
        <header
          className="relative flex items-center justify-between px-5 shrink-0 h-14 border-b border-border bg-bg-secondary"
        >
          <div className="shell-divider pointer-events-none absolute inset-x-10 top-0 h-px" />
          {/* Center: brand tagline (the pre-redesign header treatment) */}
          <span
            className="hidden md:block absolute left-1/2 -translate-x-1/2 text-2xs font-semibold uppercase tracking-[0.18em] text-text-tertiary pointer-events-none select-none whitespace-nowrap"
            aria-hidden="true"
          >
            Memory layer for AI coding agents
          </span>
          {/* Left: Hamburger (mobile) + page area */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              className="md:hidden flex items-center justify-center w-8 h-8 rounded-md transition-colors hover:bg-surface-hover"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open navigation menu"
            >
              <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <line x1="3" y1="5" x2="17" y2="5" />
                <line x1="3" y1="10" x2="17" y2="10" />
                <line x1="3" y1="15" x2="17" y2="15" />
              </svg>
            </button>
          </div>

          {/* Right: ThemeToggle + Account menu */}
          <div className="flex items-center gap-2 shrink-0">
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
            <Dropdown
              trigger={(open) => (
                <button
                  className="flex items-center gap-2 rounded-md px-2 py-1 transition-colors hover:bg-surface-hover"
                  aria-haspopup="menu"
                  aria-expanded={open}
                >
                  <span
                    className="flex items-center justify-center w-7 h-7 rounded-full text-xs font-semibold bg-brand text-text-inverse"
                  >
                    {userInitial}
                  </span>
                  <Badge variant={tierVariant as 'default' | 'success' | 'info'} label={tierLabel} size="sm" />
                </button>
              )}
              items={accountMenuItems}
              onSelect={handleAccountMenuSelect}
              menuLabel="Account menu"
              minWidthClass="min-w-[240px]"
            />
          </div>
        </header>

        {/* ── Mobile navigation drawer ── */}
        <div
          className={`fixed inset-0 z-50 md:hidden ${drawerOpen ? '' : 'pointer-events-none'}`}
          role="dialog"
          aria-modal="true"
          aria-label="Navigation menu"
        >
          {/* Backdrop */}
          <div
            className={`absolute inset-0 bg-black transition-opacity duration-200 ${
              drawerOpen ? 'opacity-50' : 'opacity-0'
            }`}
            onClick={() => setDrawerOpen(false)}
          />
          {/* Drawer panel */}
          <nav
            aria-label="Primary"
            className={`absolute top-0 left-0 bottom-0 w-64 flex flex-col transition-transform duration-200 ease-in-out bg-bg-secondary border-r border-border ${
              drawerOpen ? 'translate-x-0' : '-translate-x-full'
            }`}
          >
            {/* Drawer header */}
            <div className="flex items-center justify-between px-4 h-14">
              <Wordmark size="sm" />
              <button
                onClick={() => setDrawerOpen(false)}
                className="flex items-center justify-center w-8 h-8 rounded-md transition-colors hover:bg-surface-hover"
                aria-label="Close navigation menu"
              >
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                  <line x1="4" y1="4" x2="14" y2="14" />
                  <line x1="14" y1="4" x2="4" y2="14" />
                </svg>
              </button>
            </div>
            <div className="border-t border-border" />
            {/* Drawer links — full-width (always non-collapsed on mobile) */}
            <div className="flex flex-col flex-1 py-2 px-2 gap-0.5 overflow-y-auto">
              {filteredGroups.map((group, gi) => (
                <div key={gi}>
                  {group.label && (
                    <div className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mt-4 mb-1 px-2">
                      {group.label}
                    </div>
                  )}
                  {group.items.map((item) => {
                    const active = item.match(location.pathname);
                    const bc = badgeCount(item.badgeKey);
                    return (
                      <Link
                        key={item.to}
                        to={item.to}
                        onClick={() => setDrawerOpen(false)}
                        className={`flex items-center gap-2.5 px-3 py-2.5 rounded-md text-base transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
                          active
                            ? 'bg-surface-hover text-brand font-medium'
                            : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary'
                        }`}
                      >
                        <span className="shrink-0 flex items-center justify-center w-4 h-4">{item.icon}</span>
                        <span className="flex-1">{item.label}</span>
                        {bc != null && bc > 0 && (
                          <span
                            className="px-1.5 py-0.5 text-xs rounded-full font-medium"
                            style={{ backgroundColor: 'rgba(240,192,64,0.15)', color: 'var(--warning)' }}
                            role="status"
                            aria-label={`${bc} pending`}
                          >
                            {bc}
                          </span>
                        )}
                      </Link>
                    );
                  })}
                </div>
              ))}
              <div className="flex-1" />
              {filteredBottom.map((item) => {
                const active = item.match(location.pathname);
                return (
                  <Link
                    key={item.to}
                    to={item.to}
                    onClick={() => setDrawerOpen(false)}
                    className={`flex items-center gap-2.5 px-3 py-2.5 rounded-md text-base transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
                      active
                        ? 'bg-surface-hover text-brand font-medium'
                        : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary'
                    }`}
                  >
                    <span className="shrink-0 flex items-center justify-center w-4 h-4">{item.icon}</span>
                    <span>{item.label}</span>
                  </Link>
                );
              })}
            </div>
          </nav>
        </div>

        {/* ── Main content ── */}
        <main className="relative flex-1 bg-bg-primary overflow-x-clip">
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

        {/* ── Footer ── */}
        <footer className="text-center py-8 text-2xs text-text-tertiary border-t border-border">
          <span className="font-semibold text-text-secondary">SessionFS</span>
          <span className="mx-2 text-text-tertiary">·</span>
          Memory layer for AI coding agents
          <span className="mx-2 text-text-tertiary">·</span>
          v{__APP_VERSION__}
          <span className="mx-2 text-text-tertiary">·</span>
          <a href={siteHref('/quickstart/')} className="text-text-tertiary hover:text-text-secondary transition-colors">Docs</a> &middot;{' '}
          <a href={siteHref('/')} className="text-text-tertiary hover:text-text-secondary transition-colors">Status</a> &middot;{' '}
          <a href="mailto:support@sessionfs.dev" className="text-text-tertiary hover:text-text-secondary transition-colors">Support</a>
        </footer>
      </div>
    </div>
  );
}
