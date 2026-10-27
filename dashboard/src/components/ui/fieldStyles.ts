/* Shared form-field styling for Input, Textarea, Select and Combobox. */

export const baseField =
  'w-full bg-bg-sunken border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary transition-[background-color,border-color,box-shadow] duration-150 ease-out outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 disabled:cursor-not-allowed';

export const errorField =
  'border-[var(--danger)] focus-visible:border-[var(--danger)] focus-visible:shadow-[0_0_0_3px_color-mix(in_srgb,var(--danger)_25%,transparent)]';

/** Join the caller's aria-describedby with the hint/error ids the wrapper renders. */
export function describedBy(controlId: string, own: string | undefined, hint: unknown, error: unknown): string | undefined {
  const ids = [own, hint ? `${controlId}-hint` : null, error ? `${controlId}-error` : null].filter(Boolean);
  return ids.length ? ids.join(' ') : undefined;
}

