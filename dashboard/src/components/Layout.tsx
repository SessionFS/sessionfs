import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useHandoffInbox } from '../hooks/useHandoffs';
import { useMe } from '../hooks/useMe';
import SearchBar from './SearchBar';

export default function Layout() {
  const { logout } = useAuth();
  const location = useLocation();
  const inbox = useHandoffInbox();
  const me = useMe();
  const isAdmin = me.data?.tier === 'admin';
  const pendingCount = inbox.data?.handoffs.filter((h) => h.status === 'pending').length ?? 0;

  return (
    <div className="flex flex-col min-h-screen">
      <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-bg-secondary">
        <Link to="/" className="flex items-center shrink-0 hover:opacity-90 transition-opacity">
          <svg viewBox="0 0 180 50" className="h-6" xmlns="http://www.w3.org/2000/svg">
            <path d="M22,8 Q14,8 14,16 L14,34 Q14,42 22,42" fill="none" stroke="#4f9cf7" strokeWidth="3" strokeLinecap="round"/>
            <path d="M42,8 Q50,8 50,16 L50,34 Q50,42 42,42" fill="none" stroke="#3ddc84" strokeWidth="3" strokeLinecap="round"/>
            <circle cx="32" cy="25" r="5" fill="#4f9cf7"/>
            <text x="60" y="31" fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" fontSize="19" fontWeight="500" fill="#e6edf3" letterSpacing="-0.5">Session<tspan fill="#4f9cf7">FS</tspan></text>
          </svg>
        </Link>
        <SearchBar />
        <nav className="flex items-center gap-4 text-[15px] shrink-0">
          <Link
            to="/"
            className={`hover:text-accent transition-colors ${location.pathname === '/' ? 'text-accent' : 'text-[#b1b8c1]'}`}
          >
            Sessions
          </Link>
          <Link
            to="/handoffs"
            className={`hover:text-accent transition-colors ${location.pathname.startsWith('/handoffs') ? 'text-accent' : 'text-[#b1b8c1]'}`}
          >
            Handoffs
            {pendingCount > 0 && (
              <span className="ml-1 px-1.5 py-0.5 text-sm bg-yellow-500/20 text-yellow-400 rounded-full">
                {pendingCount}
              </span>
            )}
          </Link>
          {isAdmin && (
            <Link
              to="/admin"
              className={`hover:text-accent transition-colors ${location.pathname === '/admin' ? 'text-accent' : 'text-[#b1b8c1]'}`}
            >
              Admin
            </Link>
          )}
          <Link
            to="/settings"
            className={`hover:text-accent transition-colors ${location.pathname === '/settings' ? 'text-accent' : 'text-[#b1b8c1]'}`}
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
