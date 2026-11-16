import { useState, useEffect, type ReactNode } from 'react';
import { applyTheme, getStoredTheme, getTheme, type Theme } from '../utils/theme';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useHandoffInbox } from '../hooks/useHandoffs';
import { useMe } from '../hooks/useMe';
import { useTransfers } from '../transfers/useTransfers';
import { useMyInvites } from '../invites/useInvites';
import { useNeedsYouCount } from '../hooks/useReviewLoop';
import { useTicketNotificationUnreadCount } from '../hooks/useTicketNotifications';
import { useRemindersDueCount } from '../hooks/useReminders';
import { useForYouCount } from '../hooks/useForYou';
import { prefersSessionsHome } from '../foryou/model';
import { useMyApprovalsCount } from '../hooks/useApprovals';
import SearchBar from './SearchBar';
import ThemeToggle from './ThemeToggle';
import { Badge } from './Badge';
import { Dropdown, Icon, IconButton, Tooltip } from './ui';
import {
  AlarmClock, ArrowLeftRight, Bell, Building2, ChevronsLeft, ChevronsRight, CircleHelp, CreditCard, Folder,
  LogOut, Menu, MessageSquare, Repeat, Send, Settings, Shield, ShieldCheck, Sparkles, Star, Store, UserPlus, X,
} from 'lucide-react';
import Wordmark from './Wordmark';
import { displayTier } from '../utils/tier';
import { useHelper } from '../helper/HelperProvider';

// ── Sidebar nav item icons ──
const forYouIcon = <Icon icon={Star} />;
const sessionsIcon = <Icon icon={MessageSquare} />;
const projectsIcon = <Icon icon={Folder} />;
const marketplaceIcon = <Icon icon={Store} />;
const handoffsIcon = <Icon icon={Send} />;
const transfersIcon = <Icon icon={ArrowLeftRight} />;
const invitesIcon = <Icon icon={UserPlus} />;
const loopsIcon = <Icon icon={Repeat} />;
const orgIcon = <Icon icon={Building2} />;
const billingIcon = <Icon icon={CreditCard} />;
const settingsIcon = <Icon icon={Settings} />;
const helpIcon = <Icon icon={CircleHelp} />;
const bellIcon = <Icon icon={Bell} />;
const remindersIcon = <Icon icon={AlarmClock} />;
const approvalsIcon = <Icon icon={ShieldCheck} />;
const adminIcon = <Icon icon={Shield} />;
const collapseIcon = <Icon icon={ChevronsLeft} />;
const expandIcon = <Icon icon={ChevronsRight} />;
const helperIcon = <Icon icon={Sparkles} />;

// ── Nav config ──
interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  match: (p: string) => boolean;
  orgOnly?: boolean;
  adminOnly?: boolean;
  badgeKey?: 'pending' | 'transfers' | 'invites' | 'notifications' | 'loops' | 'reminders' | 'foryou' | 'approvals';
}

interface NavGroup {
  label?: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    items: [
      {
        to: '/',
        label: 'For You',
        icon: forYouIcon,
        match: (p: string) => p === '/for-you' || (p === '/' && !prefersSessionsHome()),
        badgeKey: 'foryou',
      },
      {
        to: '/sessions',
        label: 'Sessions',
        icon: sessionsIcon,
        match: (p: string) => p === '/sessions' || p.startsWith('/sessions/') || (p === '/' && prefersSessionsHome()),
      },
      { to: '/projects', label: 'Projects', icon: projectsIcon, match: (p: string) => p.startsWith('/projects') },
      { to: '/handoffs', label: 'Handoffs', icon: handoffsIcon, match: (p: string) => p.startsWith('/handoffs'), badgeKey: 'pending' },
    ],
  },
  {
    label: 'Library',
    items: [
      { to: '/marketplace', label: 'Marketplace', icon: marketplaceIcon, match: (p: string) => p.startsWith('/marketplace') },
    ],
  },
  {
    label: 'Inbox',
    items: [
      { to: '/inbox', label: 'Notifications', icon: bellIcon, match: (p: string) => p.startsWith('/inbox'), badgeKey: 'notifications' },
      { to: '/reminders', label: 'Reminders', icon: remindersIcon, match: (p: string) => p.startsWith('/reminders'), badgeKey: 'reminders' },
      { to: '/approvals', label: 'Approvals', icon: approvalsIcon, match: (p: string) => p.startsWith('/approvals'), badgeKey: 'approvals' },
      { to: '/review-loops', label: 'Review loops', icon: loopsIcon, match: (p: string) => p.startsWith('/review-loops'), badgeKey: 'loops' },
      { to: '/invites', label: 'Invites', icon: invitesIcon, match: (p: string) => p.startsWith('/invites'), badgeKey: 'invites' },
      { to: '/transfers', label: 'Transfers', icon: transfersIcon, match: (p: string) => p.startsWith('/transfers'), badgeKey: 'transfers' },
    ],
  },
  {
    label: 'Manage',
    items: [
      { to: '/settings/organization', label: 'Organization', icon: orgIcon, match: (p: string) => p === '/settings/organization' || p.startsWith('/settings/organization/'), orgOnly: true },
      { to: '/settings/billing', label: 'Billing', icon: billingIcon, match: (p: string) => p === '/settings/billing' },
    ],
  },
];

const BOTTOM_ITEMS: NavItem[] = [
  {
    to: '/settings',
    label: 'Settings',
    icon: settingsIcon,
    match: (p: string) =>
      p.startsWith('/settings') && p !== '/settings/billing' && p !== '/settings/organization' && !p.startsWith('/settings/organization/'),
  },
  { to: '/help', label: 'Help', icon: helpIcon, match: (p: string) => p === '/help' },
  { to: '/admin', label: 'Admin', icon: adminIcon, match: (p: string) => p === '/admin', adminOnly: true },
];

/** For You lives at "/" unless the person chose Sessions as home. */
function navHref(item: NavItem): string {
  return item.badgeKey === 'foryou' && prefersSessionsHome() ? '/for-you' : item.to;
}

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
      to={navHref(item)}
      onClick={onClick}
      className={`flex items-center gap-2.5 rounded-md text-sm font-medium transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
        collapsed ? 'justify-center w-9 h-9 mx-auto' : 'px-3 py-1.5'
      } ${
        active
          ? 'bg-surface border border-border text-text-primary'
          : 'border border-transparent text-text-secondary hover:text-text-primary hover:bg-surface-hover'
      }`}
      aria-current={active ? 'page' : undefined}
      // Collapsed links show only an icon, and the tooltip only describes the
      // link while it is open, so name the link here.
      aria-label={
        collapsed ? (badgeCount ? `${item.label} (${badgeCount} pending)` : item.label) : undefined
      }
    >
      <span className="shrink-0 flex items-center justify-center w-4 h-4">{item.icon}</span>
      {!collapsed && (
        <span className="truncate">{item.label}</span>
      )}
      {!collapsed && badgeCount != null && badgeCount > 0 && (
        <span
          className="ml-auto px-1.5 py-0.5 text-xs rounded-full font-medium tabular-nums"
          style={{ backgroundColor: 'color-mix(in srgb, var(--warning) 15%, transparent)', color: 'var(--warning)' }}
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
  const navigate = useNavigate();
  const location = useLocation();
  const inbox = useHandoffInbox();
  const me = useMe();
  const { openHelper } = useHelper();
  const isAdmin = me.data?.tier === 'admin';
  const hasOrg = !!me.data?.org_id;
  const pendingCount = inbox.data?.handoffs.filter((h) => h.status === 'pending').length ?? 0;
  const incomingTransfers = useTransfers('incoming', 'pending');
  const transfersPendingCount = incomingTransfers.data?.transfers.length ?? 0;
  const myInvites = useMyInvites();
  const invitesPendingCount = myInvites.data?.invites.length ?? 0;
  const notificationCount = useTicketNotificationUnreadCount();
  const notificationsUnread = notificationCount.data?.count ?? 0;

  const loopsNeedYou = useNeedsYouCount().data?.count ?? 0;
  const remindersDue = useRemindersDueCount().data?.count ?? 0;
  const forYouCount = useForYouCount().data?.actionable ?? 0;
  const approvalsWaiting = useMyApprovalsCount().data?.count ?? 0;

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
  const [theme, setTheme] = useState<Theme>(getTheme);

  // While no explicit choice is stored, follow OS theme changes live.
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => {
      if (getStoredTheme()) return;
      const next: Theme = mq.matches ? 'dark' : 'light';
      applyTheme(next);
      setTheme(next);
    };
    mq.addEventListener?.('change', onChange);
    return () => mq.removeEventListener?.('change', onChange);
  }, []);

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
      case 'notifications': return notificationsUnread;
      case 'loops': return loopsNeedYou;
      case 'reminders': return remindersDue;
      case 'foryou': return forYouCount;
      case 'approvals': return approvalsWaiting;
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
  const tierLabel = displayTier(me.data);
  const tierVariant = tierLabel === 'admin' ? 'info' : tierLabel === 'team' ? 'success' : 'default';
  const userInitial = (me.data?.email?.[0] || 'U').toUpperCase();

  const tierBadgeEl = (
    <Badge variant={tierVariant as 'default' | 'success' | 'info'} label={tierLabel} size="sm" />
  );

  const logoutIconEl = <Icon icon={LogOut} />;

  // Account menu = identity + account/utility items (Settings, Help) + sign-out.
  // Primary navigation (Sessions, Projects, Handoffs, Organization, Invites,
  // Billing, Admin) lives in the sidebar and is intentionally NOT duplicated
  // here; theme has its own header toggle. Settings/Help are the conventional
  // account-menu exception and also remain pinned at the sidebar bottom.
  const accountMenuItems: Array<{ key: string; label: string; icon?: ReactNode; danger?: boolean; disabled?: boolean; separator?: boolean; header?: boolean; badge?: string }> = [
    { key: 'identity', label: me.data?.email || 'Unknown', icon: tierBadgeEl, header: true },
    { key: 'settings-sep', label: '', separator: true as const },
    { key: 'settings', label: 'Settings', icon: settingsIcon },
    { key: 'help', label: 'Help', icon: helpIcon },
    { key: 'logout-sep', label: '', separator: true as const },
    { key: 'logout', label: 'Logout', icon: logoutIconEl, danger: true },
  ];

  function handleAccountMenuSelect(key: string) {
    switch (key) {
      case 'settings': navigate('/settings'); break;
      case 'help': navigate('/help'); break;
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
        aria-label="Sidebar"
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
              <span className="shrink-0 w-3.5 h-3.5"><Icon icon={Building2} size={14} /></span>
              <span className="truncate">{me.data?.org_name || me.data?.org_id || me.data?.default_org_id}</span>
            </Link>
          </div>
        )}

        {/* SearchBar — the input IS the search trigger */}
        <div className="px-2 pb-2">
          <SearchBar inputId="sidebar-search-input" className="relative w-full" />
        </div>

        {/* Ask the helper — only when the provider/drawer is available. */}
        {openHelper && (
          <div className="px-2 pb-1">
            <Tooltip content={'Ask the helper'} describe={false}>
              <button
                type="button"
                onClick={() => openHelper()}
                className={`flex items-center gap-2.5 rounded-md text-sm font-medium text-text-secondary hover:text-text-primary hover:bg-surface-hover transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] w-full ${
                  collapsed ? 'justify-center w-9 h-9 mx-auto' : 'px-3 py-1.5'
                }`}
                aria-label="Ask the helper"
              >
                <span className="shrink-0 flex items-center justify-center w-4 h-4">{helperIcon}</span>
                {!collapsed && <span className="truncate">Ask the helper</span>}
              </button>
            </Tooltip>
          </div>
        )}

        {/* Nav groups — scrollable middle section */}
        <nav aria-label="Primary" className="flex flex-col flex-1 px-2 overflow-y-auto">
          {renderNavItems()}
        </nav>

        {/* Collapse toggle — bottom pinned, non-scrollable */}
        <div className={`flex ${collapsed ? 'justify-center' : 'justify-end'} px-2 py-2`}>
          <Tooltip content={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} describe={false} side="top">
            <button
              type="button"
              onClick={toggleCollapsed}
              className="flex items-center justify-center w-7 h-7 rounded-sm hover:bg-surface-hover transition-colors text-text-tertiary hover:text-text-secondary"
              aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            >
              {collapsed ? expandIcon : collapseIcon}
            </button>
          </Tooltip>
        </div>
      </aside>

      {/* ── Content area ── */}
      <div className="flex-1 flex flex-col min-h-screen min-w-0">
        {/* ── Header (slim) ── */}
        {/* Three grid tracks: the side tracks share the free space equally so
            the tagline sits centred when there is room, and the middle track
            can shrink to nothing, so the tagline can never run under the
            actions. The tagline is hidden outright when the header (not the
            viewport: the sidebar takes part of it) is too narrow to hold it
            centred beside the actions. */}
        <header
          className="@container/header relative grid grid-cols-[1fr_minmax(0,auto)_1fr] items-center gap-3 px-5 shrink-0 h-14 border-b border-border bg-bg-secondary"
        >
          <div className="shell-divider pointer-events-none absolute inset-x-10 top-0 h-px" />
          {/* Left: Hamburger (mobile) + page area */}
          <div className="col-start-1 row-start-1 flex items-center gap-2 min-w-0">
            <Tooltip content={'Open navigation menu'} describe={false} side="bottom">
              <button
                className="md:hidden flex items-center justify-center w-8 h-8 rounded-md transition-colors hover:bg-surface-hover"
                onClick={() => setDrawerOpen(true)}
                aria-label="Open navigation menu"
              >
                <Icon icon={Menu} size={20} />
              </button>
            </Tooltip>
          </div>

          {/* Center: brand tagline (the pre-redesign header treatment) */}
          <span
            className="col-start-2 row-start-1 hidden @min-[44rem]/header:block min-w-0 truncate text-center text-2xs font-semibold uppercase tracking-[0.18em] text-text-tertiary pointer-events-none select-none"
            aria-hidden="true"
          >
            Memory Layer For AI Agents
          </span>

          {/* Right: notification bell + ThemeToggle + Account menu */}
          <div className="col-start-3 row-start-1 flex items-center justify-end gap-2">
            <IconButton
              label={`Inbox${notificationsUnread > 0 ? ` (${notificationsUnread} unread)` : ''}`}
              variant="ghost"
              className="relative"
              onClick={() => navigate('/inbox')}
            >
              <span className="flex items-center justify-center w-5 h-5">{bellIcon}</span>
              {notificationsUnread > 0 && (
                <span
                  className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full text-[10px] font-semibold tabular-nums flex items-center justify-center"
                  style={{
                    backgroundColor: 'color-mix(in srgb, var(--warning) 18%, transparent)',
                    color: 'var(--warning)',
                  }}
                  role="status"
                  aria-label={`${notificationsUnread} unread`}
                >
                  {notificationsUnread > 99 ? '99+' : notificationsUnread}
                </span>
              )}
            </IconButton>
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
            aria-label="Primary (mobile)"
            className={`absolute top-0 left-0 bottom-0 w-64 flex flex-col transition-transform duration-200 ease-in-out bg-bg-secondary border-r border-border ${
              drawerOpen ? 'translate-x-0' : '-translate-x-full'
            }`}
          >
            {/* Drawer header */}
            <div className="flex items-center justify-between px-4 h-14">
              <Wordmark size="sm" />
              <Tooltip content={'Close navigation menu'} describe={false} side="bottom">
                <button
                  onClick={() => setDrawerOpen(false)}
                  className="flex items-center justify-center w-8 h-8 rounded-md transition-colors hover:bg-surface-hover"
                  aria-label="Close navigation menu"
                >
                  <Icon icon={X} />
                </button>
              </Tooltip>
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
                        to={navHref(item)}
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
                            style={{ backgroundColor: 'color-mix(in srgb, var(--warning) 15%, transparent)', color: 'var(--warning)' }}
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
          <div key={location.pathname} className="page-enter relative flex flex-col flex-1 min-h-0">
            <Outlet />
          </div>
        </main>

        {/* ── Footer ── */}
        <footer className="text-center py-8 text-2xs text-text-tertiary border-t border-border">
          <span className="font-semibold text-text-secondary">SessionFS</span>
          <span className="mx-2 text-text-tertiary">·</span>
          Memory Layer For AI Agents
          <span className="mx-2 text-text-tertiary">·</span>
          v{__APP_VERSION__}
          <span className="mx-2 text-text-tertiary">·</span>
          <a href={siteHref('/quickstart/')} className="text-text-tertiary hover:text-text-secondary transition-colors">Docs</a> &middot;{' '}
          <a href="mailto:support@sessionfs.dev" className="text-text-tertiary hover:text-text-secondary transition-colors">Support</a> &middot;{' '}
          <a href={siteHref('/terms/')} className="text-text-tertiary hover:text-text-secondary transition-colors">Terms</a> &middot;{' '}
          <a href={siteHref('/privacy/')} className="text-text-tertiary hover:text-text-secondary transition-colors">Privacy</a> &middot;{' '}
          <a href="mailto:security@sessionfs.dev" className="text-text-tertiary hover:text-text-secondary transition-colors">Security</a>
        </footer>
      </div>
    </div>
  );
}
