import { useState } from 'react';
import { useCreateHandoff } from '../hooks/useHandoffs';

interface HandoffModalProps {
  sessionId: string;
  onClose: () => void;
}

export default function HandoffModal({ sessionId, onClose }: HandoffModalProps) {
  const [email, setEmail] = useState('');
  const [message, setMessage] = useState('');
  const createHandoff = useCreateHandoff();

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    createHandoff.mutate(
      { sessionId, recipientEmail: email.trim(), message: message.trim() || undefined },
    );
  }

  function handleBackdropClick(e: React.MouseEvent) {
    if (e.target === e.currentTarget) onClose();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={handleBackdropClick}
      onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label="Hand off session"
    >
      <div className="bg-bg-secondary border border-border rounded-lg shadow-lg w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <h2 className="text-sm font-medium text-text-primary">Hand Off Session</h2>
          <button
            onClick={onClose}
            className="text-text-muted hover:text-text-secondary transition-colors text-lg leading-none"
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        {createHandoff.isSuccess ? (
          <div className="p-4">
            <div className="p-3 bg-green-500/10 border border-green-500/30 rounded mb-3">
              <p className="text-green-400 text-sm font-medium">Handoff sent</p>
              <p className="text-text-secondary text-sm mt-1">
                Handoff ID: <code className="bg-bg-primary px-1 rounded">{createHandoff.data.id}</code>
              </p>
              <p className="text-text-muted text-sm mt-1">
                Notification sent to {createHandoff.data.recipient_email}
              </p>
            </div>
            <button
              onClick={onClose}
              className="w-full px-3 py-2 text-sm bg-bg-tertiary border border-border rounded hover:border-text-muted transition-colors"
            >
              Close
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="p-4">
            <label className="block mb-3">
              <span className="text-sm text-text-muted block mb-1">Recipient email</span>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="teammate@company.com"
                className="w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
              />
            </label>

            <label className="block mb-4">
              <span className="text-sm text-text-muted block mb-1">
                Message <span className="text-text-muted">(optional)</span>
              </span>
              <textarea
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                maxLength={2000}
                rows={3}
                placeholder="Context for the recipient..."
                className="w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent resize-none"
              />
              <span className="text-sm text-text-muted mt-0.5 block text-right">
                {message.length}/2000
              </span>
            </label>

            {createHandoff.isError && (
              <div className="mb-3 p-2 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
                Failed to create handoff: {String(createHandoff.error)}
              </div>
            )}

            <div className="flex gap-2">
              <button
                type="button"
                onClick={onClose}
                className="flex-1 px-3 py-2 text-sm bg-bg-tertiary border border-border rounded hover:border-text-muted transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={createHandoff.isPending || !email.trim()}
                className="flex-1 px-3 py-2 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors disabled:opacity-50"
              >
                {createHandoff.isPending ? 'Sending...' : 'Send Handoff'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
