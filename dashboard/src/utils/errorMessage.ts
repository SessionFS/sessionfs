import { ApiError } from '../api/client';

/**
 * Turn an unknown thrown value into a clean user-facing string: no "Error: "
 * prefix, the ApiError envelope message preferred, and a fallback when
 * nothing usable is present.
 */
export function errorMessage(err: unknown, fallback = 'Something went wrong'): string {
  let msg = '';
  if (err instanceof ApiError) {
    // The envelope parser renders "code: message"; users only need the message.
    const prefix = err.code ? `${err.code}: ` : '';
    msg = prefix && err.message.startsWith(prefix) ? err.message.slice(prefix.length) : err.message;
  } else if (err instanceof Error) {
    msg = err.message;
  } else if (typeof err === 'string') {
    msg = err;
  } else if (err && typeof err === 'object' && typeof (err as { message?: unknown }).message === 'string') {
    msg = (err as { message: string }).message;
  }
  msg = msg.replace(/^(?:[A-Za-z]*Error:\s*)+/, '').trim();
  return msg || fallback;
}
