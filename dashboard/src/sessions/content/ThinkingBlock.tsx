import { useState } from 'react';

export function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg bg-bg-sunken border border-border">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full px-3 py-1.5 text-left text-xs text-text-tertiary hover:text-text-secondary transition-colors flex items-center gap-2 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        <span className="text-2xs font-mono uppercase tracking-wider">Thinking</span>
        <span className="text-text-tertiary">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <pre className="px-3 py-2 border-t border-border text-xs text-text-tertiary whitespace-pre-wrap max-h-60 overflow-y-auto leading-relaxed">
          {text}
        </pre>
      )}
    </div>
  );
}
