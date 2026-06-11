import { useState } from 'react';
import { useCreateHandoff } from '../hooks/useHandoffs';
import { handoffSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import { Dialog, DialogHeader, DialogFooter, Input, Textarea, Button } from '../components/ui';

interface HandoffModalProps {
  sessionId: string;
  onClose: () => void;
}

export default function HandoffModal({ sessionId, onClose }: HandoffModalProps) {
  const [email, setEmail] = useState('');
  const [message, setMessage] = useState('');
  const [errors, setErrors] = useState<FieldErrors>({});
  const createHandoff = useCreateHandoff();

  function validateField(field: 'recipient_email' | 'message', value: string) {
    const data = { recipient_email: field === 'recipient_email' ? value : email, message: field === 'message' ? value : message };
    const result = handoffSchema.safeParse(data);
    if (!result.success) {
      const fieldErrors = fieldErrorsFromZod(result.error);
      setErrors((prev) => ({ ...prev, [field]: fieldErrors[field] }));
    } else {
      setErrors((prev) => ({ ...prev, [field]: undefined }));
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const result = handoffSchema.safeParse({ recipient_email: email.trim(), message: message.trim() || undefined });
    if (!result.success) {
      setErrors(fieldErrorsFromZod(result.error));
      return;
    }
    setErrors({});
    createHandoff.mutate(
      { sessionId, recipientEmail: email.trim(), message: message.trim() || undefined },
    );
  }

  const titleId = 'handoff-dialog-title';

  return (
    <Dialog open onClose={onClose} titleId={titleId} className="max-w-md">
      <DialogHeader titleId={titleId}>Hand Off Session</DialogHeader>

      {createHandoff.isSuccess ? (
        <>
          <div className="p-3 rounded-lg mb-4" style={{ backgroundColor: 'rgba(61,220,132,0.1)', border: '1px solid rgba(61,220,132,0.3)' }}>
            <p className="text-[var(--accent)] text-sm font-medium">Handoff sent</p>
            <p className="text-[var(--text-secondary)] text-sm mt-1">
              Handoff ID: <code className="text-mono-chip">{createHandoff.data.id}</code>
            </p>
            <p className="text-[var(--text-tertiary)] text-sm mt-1">
              Notification sent to {createHandoff.data.recipient_email}
            </p>
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={onClose}>Close</Button>
          </DialogFooter>
        </>
      ) : (
        <form onSubmit={handleSubmit}>
          <div className="space-y-4 mb-4">
            <Input
              type="email"
              required
              title="Recipient email"
              placeholder="teammate@company.com"
              value={email}
              onChange={(e) => { setEmail(e.target.value); if (errors.recipient_email) setErrors((prev) => ({ ...prev, recipient_email: undefined })); }}
              onBlur={() => { if (email.trim()) validateField('recipient_email', email); }}
              error={errors.recipient_email}
            />

            <div>
              <Textarea
                title="Message (optional)"
                value={message}
                onChange={(e) => { setMessage(e.target.value); if (errors.message) setErrors((prev) => ({ ...prev, message: undefined })); }}
                onBlur={() => { if (message) validateField('message', message); }}
                maxLength={2000}
                rows={3}
                placeholder="Context for the recipient…"
                error={errors.message}
              />
              <span className="text-xs text-[var(--text-tertiary)] mt-0.5 block text-right">
                {message.length}/2000
              </span>
            </div>
          </div>

          {createHandoff.isError && (
            <div className="mb-4 p-2 rounded text-sm" style={{ backgroundColor: 'rgba(240,64,96,0.1)', color: 'var(--danger)', border: '1px solid rgba(240,64,96,0.3)' }}>
              Failed to create handoff: {String(createHandoff.error)}
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={createHandoff.isPending || !email.trim()}
              loading={createHandoff.isPending}
            >
              Send Handoff
            </Button>
          </DialogFooter>
        </form>
      )}
    </Dialog>
  );
}
