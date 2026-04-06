import { z } from 'zod';

// ── Shared schemas ──────────────────────────────────────────────────

/** Git remote: non-empty, looks like a URL or owner/repo shorthand. */
export const createProjectSchema = z.object({
  git_remote_normalized: z
    .string()
    .trim()
    .min(1, 'Git remote URL is required')
    .refine(
      (v) => /^https?:\/\/.+\/.+/.test(v) || /^git@.+:.+\/.+/.test(v) || /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(v) || /^[A-Za-z0-9_.-]+\.[a-z]+\/.+\/.+$/.test(v),
      'Must be a git URL (https://...) or owner/repo',
    ),
});

/** Handoff: valid email, optional message capped at 2000 chars. */
export const handoffSchema = z.object({
  recipient_email: z.string().trim().min(1, 'Recipient email is required').email('Must be a valid email address'),
  message: z.string().max(2000, 'Message must be 2000 characters or fewer').optional(),
});

/** Login: API key must start with sk_sfs_, base URL must be valid. */
export const loginSchema = z.object({
  apiKey: z.string().min(1, 'API key is required').refine((v) => v.startsWith('sk_sfs_'), 'API key must start with "sk_sfs_"'),
  baseUrl: z.string().url('Must be a valid URL'),
});

/** Signup: valid email. */
export const signupSchema = z.object({
  email: z.string().trim().min(1, 'Email is required').email('Must be a valid email address'),
  baseUrl: z.string().url('Must be a valid URL'),
});

/** Judge settings: provider, model required; API key or base URL required. */
export const judgeSettingsSchema = z
  .object({
    provider: z.string().min(1, 'Provider is required'),
    model: z.string().min(1, 'Model is required'),
    apiKey: z.string().optional(),
    baseUrl: z
      .string()
      .optional()
      .refine((v) => !v || /^https?:\/\//.test(v), 'Must be a valid URL starting with http(s)://'),
  })
  .refine((d) => (d.apiKey && d.apiKey.length > 0) || (d.baseUrl && d.baseUrl.length > 0), {
    message: 'Either an API key or base URL is required',
    path: ['apiKey'],
  });

// ── Field error helper ──────────────────────────────────────────────

export type FieldErrors = Record<string, string | undefined>;

/**
 * Extract per-field error messages from a ZodError.
 * Returns a flat map of { fieldName: firstErrorMessage }.
 */
export function fieldErrorsFromZod(error: z.ZodError): FieldErrors {
  const out: FieldErrors = {};
  for (const issue of error.issues) {
    const key = issue.path.join('.');
    if (key && !out[key]) {
      out[key] = issue.message;
    }
  }
  return out;
}
