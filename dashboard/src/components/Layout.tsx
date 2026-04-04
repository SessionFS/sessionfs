import { useState, useRef, useEffect } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useHandoffInbox } from '../hooks/useHandoffs';
import { useMe } from '../hooks/useMe';
import SearchBar from './SearchBar';
import ThemeToggle from './ThemeToggle';
import { Badge } from './Badge';

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

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (avatarRef.current && !avatarRef.current.contains(e.target as Node)) {
        setAvatarOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const tierLabel = me.data?.tier || 'free';
  const tierVariant = tierLabel === 'admin' ? 'info' : tierLabel === 'team' ? 'success' : 'default';
  const userInitial = (me.data?.email?.[0] || 'U').toUpperCase();

  return (
    <div className="flex flex-col min-h-screen">
      <header
        className="flex items-center justify-between px-5"
        style={{
          height: 56,
          borderBottom: '1px solid var(--border)',
          backgroundColor: 'var(--bg-secondary)',
        }}
      >
        {/* Left: Logo */}
        <Link to="/" className="flex items-center shrink-0 hover:opacity-90 transition-opacity">
          <span className="text-[17px] tracking-tight">
            <span className="text-[var(--text-primary)] font-bold">Session</span>
            <span className="text-[var(--brand)] font-bold">FS</span>
          </span>
        </Link>

        {/* Center: Nav links */}
        <nav className="flex items-center gap-1 text-[14px] overflow-x-auto whitespace-nowrap">
          {NAV_LINKS.map(({ to, label, match }) => {
            const active = match(location.pathname);
            const hiddenOnMobile = label === 'Billing';
            return (
              <Link
                key={to}
                to={to}
                className={`px-3 pb-[14px] pt-[16px] border-b-2 transition-colors ${
                  active
                    ? 'border-[var(--brand)] text-[var(--brand)]'
                    : 'border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                }${hiddenOnMobile ? ' hidden sm:inline-flex' : ''}`}
              >
                {label}
                {label === 'Handoffs' && pendingCount > 0 && (
                  <span
                    className="ml-1 px-1.5 py-0.5 text-xs rounded-full font-medium"
                    style={{
                      backgroundColor: 'rgba(240,192,64,0.15)',
                      color: 'var(--warning)',
                    }}
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
              className={`hidden sm:inline-flex px-3 pb-[14px] pt-[16px] border-b-2 transition-colors ${
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

        {/* Right: ThemeToggle + Avatar */}
        <div className="flex items-center gap-2 shrink-0">
          <ThemeToggle />
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
      <main className="flex-1 bg-[var(--bg-primary)]">
        <div key={location.pathname} className="page-enter flex flex-col flex-1 min-h-0">
          <Outlet />
        </div>
      </main>
      <footer className="text-center py-8 text-[11px] text-[var(--text-tertiary)] border-t border-[var(--border)]">
        SessionFS v0.9.7 &middot;{' '}
        <a href={siteHref('/quickstart/')} className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Docs</a> &middot;{' '}
        <a href={siteHref('/')} className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Status</a> &middot;{' '}
        <a href="mailto:support@sessionfs.dev" className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors">Support</a>
      </footer>
    </div>
  );
}
