/**
 * Resolve the SessionFS API base URL for the current environment.
 *
 * Shared by the login page and the SSO callback so both target the same API
 * host (the dashboard is served from app.<domain>, the API from api.<domain>).
 */
export function resolveApiBaseUrl(): string {
  // 1. Build-time env var wins.
  if (import.meta.env.VITE_API_URL) return import.meta.env.VITE_API_URL as string;
  // 2. Local dev.
  if (window.location.hostname === 'localhost') return 'http://localhost:8000';
  // 3. Production: app.<domain> → api.<domain>.
  const host = window.location.hostname;
  if (host.startsWith('app.')) {
    return `${window.location.protocol}//api.${host.slice(4)}`;
  }
  // 4. Last resort — same origin (self-hosted single-host deploys).
  return window.location.origin;
}
