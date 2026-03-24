import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useHandoffInbox } from '../hooks/useHandoffs';
import SearchBar from './SearchBar';

export default function Layout() {
  const { logout } = useAuth();
  const location = useLocation();
  const inbox = useHandoffInbox();
  const pendingCount = inbox.data?.handoffs.filter((h) => h.status === 'pending').length ?? 0;

  return (
    <div className="flex flex-col min-h-screen">
      <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-bg-secondary">
        <Link to="/" className="text-text-primary font-semibold text-sm hover:text-accent transition-colors shrink-0">
          SessionFS
        </Link>
        <SearchBar />
        <nav className="flex items-center gap-4 text-sm shrink-0">
          <Link
            to="/"
            className={`hover:text-accent transition-colors ${location.pathname === '/' ? 'text-accent' : 'text-text-secondary'}`}
          >
            Sessions
          </Link>
          <Link
            to="/handoffs"
            className={`hover:text-accent transition-colors ${location.pathname.startsWith('/handoffs') ? 'text-accent' : 'text-text-secondary'}`}
          >
            Handoffs
            {pendingCount > 0 && (
              <span className="ml-1 px-1.5 py-0.5 text-xs bg-yellow-500/20 text-yellow-400 rounded-full">
                {pendingCount}
              </span>
            )}
          </Link>
          <Link
            to="/settings"
            className={`hover:text-accent transition-colors ${location.pathname === '/settings' ? 'text-accent' : 'text-text-secondary'}`}
          >
            Settings
          </Link>
          <button
            onClick={logout}
            className="text-text-muted hover:text-text-secondary transition-colors"
          >
            Logout
          </button>
        </nav>
      </header>
      <main className="flex-1">
        <Outlet />
      </main>
    </div>
  );
}
