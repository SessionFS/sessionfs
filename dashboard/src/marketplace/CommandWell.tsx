import { useToast } from '../hooks/useToast';

/** A command in a code well: never wraps mid-token (scrolls sideways) and
 *  has its own Copy button that confirms with a toast. */
export function CommandWell({ command, label, copiedMessage = 'Command copied' }: { command: string; label: string; copiedMessage?: string }) {
  const { addToast } = useToast();
  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      addToast('success', copiedMessage);
    } catch {
      addToast('error', 'Could not copy. Select the text and copy it instead.');
    }
  }
  return (
    <div className="flex items-stretch overflow-hidden rounded-lg border border-border bg-bg-sunken">
      <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre px-3 py-2 font-mono text-xs leading-5 text-text-primary" tabIndex={0} role="group" aria-label={label}>
        {command}
      </pre>
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={`Copy: ${label}`}
        className="shrink-0 border-l border-border px-3 text-xs font-medium text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary outline-none focus-visible:shadow-[inset_0_0_0_2px_var(--brand-glow)]"
      >
        Copy
      </button>
    </div>
  );
}


