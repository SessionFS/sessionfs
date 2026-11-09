import { Fragment } from 'react';
import { Button, Dialog, DialogFooter, DialogHeader, Kbd } from '../../components/ui';
import { SHORTCUTS } from './useTicketKeys';

/* ── Keyboard shortcuts (?) ── */

export function ShortcutsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const titleId = 'tk-shortcuts-title';
  return (
    <Dialog open={open} onClose={onClose} titleId={titleId} className="max-w-2xl">
      <DialogHeader titleId={titleId}>Keyboard shortcuts</DialogHeader>
      <dl className="tk-keys">
        {SHORTCUTS.map((s) => (
          <div key={s.label} className="tk-keys-row">
            <dt>{s.label}</dt>
            <dd>
              {s.keys.map((k, i) => (
                <Fragment key={k}>
                  {i > 0 && s.sep && <span className="tk-keys-sep">{s.sep}</span>}
                  <Kbd>{k}</Kbd>
                </Fragment>
              ))}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-4 text-xs text-text-tertiary">
        Shortcuts work on the Tickets tab when you aren’t typing in a field.
      </p>
      <DialogFooter>
        <Button variant="secondary" type="button" onClick={onClose}>
          Close
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
