import { useEffect, useRef, useState } from 'react';
import { IconButton } from './IconButton';

interface MonoTextProps {
  /** Full value; always available via the title tooltip and the copy button. */
  value: string;
  /** Characters kept visible at the end when the value is truncated. */
  tailChars?: number;
  /** Show a copy button next to the value. */
  copyable?: boolean;
  className?: string;
}

/**
 * Monospace string that truncates in the middle: the start shrinks with an
 * ellipsis while the tail stays visible (the end of a git remote or id is
 * usually the distinguishing part). min-w-0 / max-w-full keep it inside its
 * container at any width, so it never forces horizontal overflow.
 */
export function MonoText({ value, tailChars = 12, copyable = false, className = '' }: MonoTextProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const tail = Math.min(tailChars, Math.floor(value.length / 2));
  const head = value.slice(0, value.length - tail);
  const end = value.slice(value.length - tail);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      return;
    }
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1500);
  }

  return (
    <span className={`inline-flex items-center gap-1.5 min-w-0 max-w-full overflow-hidden align-middle ${className}`.trim()}>
      <span className="flex min-w-0 overflow-hidden font-mono whitespace-pre" title={value}>
        <span className="min-w-0 truncate">{head}</span>
        {end && <span className="shrink-0">{end}</span>}
      </span>
      {copyable && (
        <>
          <IconButton label="Copy" size="sm" onClick={handleCopy} className="shrink-0">
            <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="6" y="6" width="11" height="11" rx="1.5" />
              <path d="M13 6V4.5A1.5 1.5 0 0 0 11.5 3h-7A1.5 1.5 0 0 0 3 4.5v7A1.5 1.5 0 0 0 4.5 13H6" />
            </svg>
          </IconButton>
          <span role="status" className="text-2xs text-success shrink-0">
            {copied ? 'Copied' : ''}
          </span>
        </>
      )}
    </span>
  );
}
