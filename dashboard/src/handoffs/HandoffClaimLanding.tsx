import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { resolveApiBaseUrl } from '../auth/apiBase';
import { Badge } from '../components/Badge';
import Wordmark from '../components/Wordmark';
import ToolIcon from '../components/ToolIcon';
import { Card, Button } from '../components/ui';

interface PreviewMessage {
  role: string;
  text: string;
  index: number;
}

interface HandoffPreview {
  title: string | null;
  sender_email: string;
  tool: string | null;
  message_count: number | null;
  status: string;
  expires_at: string;
  preview_messages: PreviewMessage[];
}

const STATUS_VARIANT: Record<string, 'warning' | 'success' | 'danger'> = {
  pending: 'warning',
};

/**
 * Public landing page for a handoff. The recipient sees a session preview
 * BEFORE authenticating — signup becomes the CLAIM action, not the view gate.
 *
 * The preview token travels in the URL fragment (`#t=...`) so it never hits
 * server access logs or Referer headers. We read it, then immediately clear
 * the fragment via history.replaceState before any async work — matching the
 * SsoCallback security model (F2).
 *
 * Security:
 * - Fragment cleared before any async work (no analytics/Referer leak)
 * - Token is sha256-at-rest on the server; raw token only in this email link
 * - Constant dead-link state for revoked/expired/already-claimed handoffs
 * - Never load the raw session archive, attachments, or API keys
 */
export default function HandoffClaimLanding() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [preview, setPreview] = useState<HandoffPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    // Read the fragment, then WIPE it before any async work.
    const rawHash = window.location.hash.startsWith('#')
      ? window.location.hash.slice(1)
      : '';
    const params = new URLSearchParams(rawHash);
    const token = params.get('t');
    window.history.replaceState(null, '', window.location.pathname);

    if (!token) {
      setError('This link is missing the preview token. Please ask the sender to resend the handoff.');
      setLoading(false);
      return;
    }

    const base = resolveApiBaseUrl();
    (async () => {
      try {
        const resp = await fetch(
          `${base}/api/v1/handoffs/${encodeURIComponent(id!)}/preview?token=${encodeURIComponent(token)}`,
        );
        if (!resp.ok) {
          // All failure modes return 404; surface a generic dead-link state.
          setError(
            'This handoff link is no longer valid. It may have been claimed, revoked, or expired.',
          );
          setLoading(false);
          return;
        }
        const data = (await resp.json()) as HandoffPreview;
        setPreview(data);
        setLoading(false);
      } catch {
        setError(
          'Could not reach the server. Please check your connection and try again.',
        );
        setLoading(false);
      }
    })();
  }, [id]);

  const formatDate = (iso: string) => {
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
    } catch {
      return iso;
    }
  };

  // --- Loading ---
  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-bg px-4">
        <Card className="w-full max-w-lg p-8 text-center space-y-5">
          <div className="flex justify-center">
            <Wordmark />
          </div>
          <p className="text-text-secondary text-sm" aria-live="polite">
            Loading handoff preview…
          </p>
        </Card>
      </div>
    );
  }

  // --- Dead-link / error state ---
  if (error || !preview) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-bg px-4">
        <Card className="w-full max-w-lg p-8 text-center space-y-5">
          <div className="flex justify-center">
            <Wordmark />
          </div>
          <h2 className="text-lg font-semibold text-text-primary">
            Handoff unavailable
          </h2>
          <p className="text-text-secondary text-sm" role="alert">
            {error || 'This handoff link is no longer valid.'}
          </p>
          <p className="text-text-tertiary text-xs">
            If you believe this is an error, ask the sender to resend the handoff.
          </p>
        </Card>
      </div>
    );
  }

  // --- Preview card ---
  const isPending = preview.status === 'pending';

  return (
    <div className="flex items-center justify-center min-h-screen bg-bg px-4 py-10">
      <Card className="w-full max-w-lg p-8 space-y-6">
        {/* Brand + sender info */}
        <div className="flex justify-center">
          <Wordmark />
        </div>

        <div className="text-center space-y-1">
          <p className="text-text-tertiary text-sm">
            <span className="font-medium text-text-secondary">{preview.sender_email}</span>
            {' '}shared a session with you
          </p>
        </div>

        {/* Session card */}
        <Card level="elevated" className="p-5 space-y-4">
          <div className="flex items-start justify-between">
            <h3 className="text-base font-semibold text-text-primary break-words flex-1">
              {preview.title || 'Untitled session'}
            </h3>
            {isPending && (
              <Badge variant={STATUS_VARIANT[preview.status] ?? 'default'} tint label={preview.status} size="sm" />
            )}
          </div>

          <div className="grid grid-cols-2 gap-3 text-sm">
            {preview.tool && (
              <div className="flex items-center gap-2">
                <ToolIcon tool={preview.tool} />
                <span className="text-text-secondary">{preview.tool}</span>
              </div>
            )}
            {preview.message_count != null && (
              <div>
                <span className="text-micro text-text-tertiary block">Messages</span>
                <span className="text-text-secondary">{preview.message_count}</span>
              </div>
            )}
          </div>

          {preview.expires_at && (
            <p className="text-xs text-text-tertiary">
              Expires {formatDate(preview.expires_at)}
            </p>
          )}
        </Card>

        {/* Message preview */}
        {preview.preview_messages.length > 0 && (
          <div className="space-y-3">
            <p className="text-sm font-medium text-text-secondary">Session preview</p>
            {preview.preview_messages.map((msg) => (
              <div
                key={msg.index}
                className="text-sm text-text-secondary px-3 py-2 rounded-lg border border-border bg-surface line-clamp-4"
              >
                <span className="text-micro text-text-tertiary block mb-0.5">
                  {msg.role === 'assistant' ? '🤖 Assistant' : msg.role === 'user' ? '👤 User' : msg.role}
                </span>
                <p className="whitespace-pre-wrap">{msg.text}</p>
              </div>
            ))}
          </div>
        )}

        {/* CTA */}
        {isPending && (
          <div className="space-y-3 pt-2">
            <Link to="/login" className="block w-full">
              <Button className="w-full">
                Sign in / create account to claim
              </Button>
            </Link>
            <p className="text-xs text-text-tertiary text-center">
              Viewing a preview — sign up to claim this handoff and access the full session.
            </p>
          </div>
        )}

        {/* Already claimed / expired — info notice */}
        {!isPending && (
          <div className="text-center space-y-2 pt-2">
            <p className="text-sm text-text-tertiary">
              This handoff is no longer open for claiming.
            </p>
            <Link to="/login" className="text-brand text-sm hover:underline">
              Sign in to SessionFS →
            </Link>
          </div>
        )}
      </Card>
    </div>
  );
}
