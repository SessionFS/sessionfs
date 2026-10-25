import { forwardRef, useLayoutEffect, useRef, type ForwardedRef } from 'react';

import { Kbd } from '../components/ui';
import { MAX_MESSAGE_CHARS } from './format';
import { ArrowUpIcon, StopIcon } from './icons';

const MAX_HEIGHT = 200;

function assignRef<T>(ref: ForwardedRef<T>, value: T) {
  if (typeof ref === 'function') ref(value);
  else if (ref) ref.current = value;
}

/**
 * Auto-growing prompt box. Enter sends, Shift+Enter adds a line (and IME
 * composition never sends). While an answer streams the box stays editable so
 * the next question can be drafted, and Send becomes Stop.
 */
const HelperComposer = forwardRef<HTMLTextAreaElement, {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  placeholder: string;
  modelLabel: string | null;
  /** True while the current scope's status is being rechecked after a switch. */
  checking?: boolean;
}>(function HelperComposer({ value, onChange, onSend, onStop, streaming, placeholder, modelLabel, checking }, ref) {
  const innerRef = useRef<HTMLTextAreaElement | null>(null);
  const over = value.length > MAX_MESSAGE_CHARS;
  const nearLimit = value.length > MAX_MESSAGE_CHARS * 0.9;
  const canSend = !streaming && !checking && value.trim().length > 0 && !over;

  useLayoutEffect(() => {
    const el = innerRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
    el.style.overflowY = el.scrollHeight > MAX_HEIGHT ? 'auto' : 'hidden';
  }, [value]);

  return (
    <div className="shrink-0 border-t border-border bg-surface px-3 pb-3 pt-3 sm:px-4">
      <div className="rounded-xl border border-border bg-bg-primary transition-[border-color,box-shadow] focus-within:border-[var(--brand)] focus-within:shadow-[0_0_0_3px_var(--brand-glow)]">
        <textarea
          ref={(el) => {
            innerRef.current = el;
            assignRef(ref, el);
          }}
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (canSend) onSend();
            }
          }}
          placeholder={placeholder}
          aria-label="Ask the session helper"
          aria-describedby={checking ? 'helper-composer-hint helper-composer-checking' : 'helper-composer-hint'}
          aria-invalid={over || undefined}
          className="block max-h-[200px] w-full resize-none bg-transparent px-3.5 pb-1 pt-3 text-base leading-6 text-text-primary placeholder:text-text-tertiary outline-none"
        />
        <div className="flex items-center gap-2 pb-2 pl-3.5 pr-2">
          <span
            id={checking ? 'helper-composer-checking' : undefined}
            className="min-w-0 flex-1 truncate text-2xs text-text-tertiary"
            title={modelLabel ?? undefined}
          >
            {checking ? 'Checking access…' : modelLabel}
          </span>
          {nearLimit && (
            <span
              className={`shrink-0 text-2xs tabular-nums ${over ? 'font-semibold text-danger' : 'text-text-tertiary'}`}
              aria-live="polite"
            >
              {value.length.toLocaleString()} / {MAX_MESSAGE_CHARS.toLocaleString()}
            </span>
          )}
          {streaming ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop"
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 text-xs font-semibold text-text-primary transition-colors hover:border-border-strong hover:bg-bg-elevated outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              <StopIcon size={11} />
              Stop
            </button>
          ) : (
            <button
              type="button"
              onClick={onSend}
              disabled={!canSend}
              aria-label="Send"
              title={over ? `Questions are limited to ${MAX_MESSAGE_CHARS.toLocaleString()} characters` : 'Send (Enter)'}
              className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-brand text-text-on-brand transition-[opacity,transform] hover:opacity-90 active:scale-95 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:cursor-not-allowed disabled:bg-surface-active disabled:text-text-tertiary disabled:active:scale-100"
            >
              <ArrowUpIcon size={16} />
            </button>
          )}
        </div>
      </div>
      <p id="helper-composer-hint" className="mt-1.5 hidden items-center gap-1 whitespace-nowrap px-1 text-2xs text-text-tertiary sm:flex">
        <Kbd>Enter</Kbd> to send
        <span aria-hidden="true" className="mx-0.5">·</span>
        <Kbd>Shift</Kbd>
        <span aria-hidden="true">+</span>
        <Kbd>Enter</Kbd> for a new line
      </p>
    </div>
  );
});

export default HelperComposer;
