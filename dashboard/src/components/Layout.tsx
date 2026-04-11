import { useState, useRef, useEffect } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useHandoffInbox } from '../hooks/useHandoffs';
import { useMe } from '../hooks/useMe';
import SearchBar from './SearchBar';
import ThemeToggle from './ThemeToggle';
import { Badge } from './Badge';
import Wordmark from './Wordmark';

const NAV_LINKS = [
  { to: '/', label: 'Sessions', match: (p: string) => p === '/' },
  { to: '/projects', label: 'Projects', match: (p: string) => p.startsWith('/projects') },
  { to: '/handoffs', label: 'Handoffs', match: (p: string) => p.startsWith('/handoffs') },
  { to: '/settings', label: 'Settings', match: (p: string) => p.startsWith('/settings') && p !== '/settings/billing' },
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
  const inbox = useHandoffInbox();
  const me = useMe();
  const isAdmin = me.data?.tier === 'admin';
  const pendingCount = inbox.data?.handoffs.filter((h) => h.status === 'pending').length ?? 0;

  const [avatarOpen, setAvatarOpen] = useState(false);
  const avatarRef = useRef<HTMLDivElement>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Close avatar dropdown on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (avatarRef.current && !avatarRef.current.contains(e.target as Node)) {
        setAvatarOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

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
    ...NAV_LINKS,
    ...(isAdmin ? [{ to: '/admin', label: 'Admin', match: (p: string) => p === '/admin' }] : []),
    { to: '/help', label: 'Help', match: (p: string) => p === '/help' },
  ];

  const tierLabel = me.data?.tier || 'free';
  const tierVariant = tierLabel === 'admin' ? 'info' : tierLabel === 'team' ? 'success' : 'default';
  const userInitial = (me.data?.email?.[0] || 'U').toUpperCase();

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
        <nav className="hidden md:flex items-center gap-1 text-[14px] overflow-x-auto whitespace-nowrap">
          {NAV_LINKS.map(({ to, label, match }) => {
            const active = match(location.pathname);
            return (
              <Link
                key={to}
                to={to}
                className={`px-3 pb-[14px] pt-[16px] border-b-2 transition-colors ${
                  active
                    ? 'border-[var(--brand)] text-[var(--brand)]'
                    : 'border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
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
              </Link>
            );
          })}
          {isAdmin && (
            <Link
              to="/admin"
              className={`px-3 pb-[14px] pt-[16px] border-b-2 transition-colors ${
                location.pathname === '/admin'
                  ? 'border-[var(--brand)] text-[var(--brand)]'
                  : 'border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
              }`}
            >
              Admin
            </Link>
          )}
          <SearchBar />
        </nav>

        {/* Right: ThemeToggle + Help + Avatar */}
        <div className="flex items-center gap-2 shrink-0">
          <ThemeToggle />
          <Link
            to="/help"
            aria-label="Help"
            className="flex items-center justify-center w-8 h-8 rounded-[var(--radius-md)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-hover)] transition-colors"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="9" />
              <path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7" />
              <line x1="12" y1="17" x2="12" y2="17.01" />
            </svg>
          </Link>
          <div className="relative" ref={avatarRef}>
            <button
              onClick={() => setAvatarOpen((v) => !v)}
              className="flex items-center gap-2 rounded-[var(--radius-md)] px-2 py-1 transition-colors hover:bg-[var(--surface-hover)]"
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
            {avatarOpen && (
              <div
                className="absolute right-0 mt-1 w-40 py-1 rounded-[var(--radius-md)] border z-50"
                style={{
                  backgroundColor: 'var(--bg-elevated)',
                  borderColor: 'var(--border)',
                  boxShadow: 'var(--shadow-md)',
                }}
              >
                <button
                  onClick={() => {
                    setAvatarOpen(false);
                    logout();
                  }}
                  className="w-full text-left px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-hover)]"
                  style={{ color: 'var(--text-secondary)' }}
                >
                  Logout
                </button>
              </div>
            )}
          </div>
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
        v0.9.8.4
        <span className="mx-2 text-[var(--text-tertiary)]">·</span>
        <a href={siteHref('/quickstart/')} className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Docs</a> &middot;{' '}
        <a href={siteHref('/')} className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Status</a> &middot;{' '}
        <a href="mailto:support@sessionfs.dev" className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Support</a>
      </footer>
    </div>
  );
}
