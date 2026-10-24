import { useState, useCallback } from 'react';
import { CopyIcon } from './icons';

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {
      // clipboard write failed — silently ignore
    });
  }, [text]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={copied ? 'Copied' : label}
      className="inline-flex items-center gap-1 rounded text-text-tertiary hover:text-text-secondary transition-colors duration-150 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
    >
      <CopyIcon />
      <span className="text-2xs">{copied ? 'Copied' : 'Copy'}</span>
    </button>
  );
}
