import { describe, it, expect } from 'vitest';
import { ApiError, parseApiErrorBody } from './client';

/**
 * v0.10.24 tk_e7da4c4508d94bac Codex R1 MED #2 — Dashboard `ApiError`
 * now parses the v0.10.x error envelope so toasts surface
 * "code: message" instead of raw JSON.
 *
 * Coverage:
 *  - envelope happy path
 *  - envelope without message (code-only fallback)
 *  - envelope without code (message-only)
 *  - legacy `detail` string shape (older FastAPI defaults)
 *  - legacy `detail` dict shape with message
 *  - plain-text body (no JSON)
 *  - empty body
 *  - constructor preserves status / code / details / raw
 */

describe('parseApiErrorBody', () => {
  it('parses the v0.10.x envelope and renders "code: message"', () => {
    const body = JSON.stringify({
      error: {
        code: 'foreign_key_violation',
        message: 'Database integrity error: a referenced row was missing.',
        details: { status: 500 },
      },
    });
    const parsed = parseApiErrorBody(body, 500);
    expect(parsed.code).toBe('foreign_key_violation');
    expect(parsed.message).toBe(
      'foreign_key_violation: Database integrity error: a referenced row was missing.',
    );
    expect(parsed.details).toEqual({ status: 500 });
  });

  it('surfaces the bare message when the envelope omits code', () => {
    const body = JSON.stringify({
      error: { message: 'Something went wrong.', code: '' },
    });
    const parsed = parseApiErrorBody(body, 500);
    expect(parsed.message).toBe('Something went wrong.');
  });

  it('falls back to code when the envelope has no message', () => {
    const body = JSON.stringify({ error: { code: 'integrity_error' } });
    const parsed = parseApiErrorBody(body, 500);
    expect(parsed.message).toBe('integrity_error');
  });

  it('handles the legacy detail-string shape', () => {
    const body = JSON.stringify({ detail: "Persona 'atlas' not found" });
    const parsed = parseApiErrorBody(body, 404);
    expect(parsed.message).toBe("Persona 'atlas' not found");
  });

  it('handles the legacy detail-dict shape with message', () => {
    const body = JSON.stringify({ detail: { message: 'Boom', code: 'BOOM' } });
    const parsed = parseApiErrorBody(body, 400);
    expect(parsed.message).toBe('Boom');
  });

  it('returns the raw text when the body is not JSON', () => {
    const parsed = parseApiErrorBody('Internal Server Error', 500);
    expect(parsed.message).toBe('Internal Server Error');
  });

  it('returns a generic message when the body is empty', () => {
    const parsed = parseApiErrorBody('', 500);
    expect(parsed.message).toBe('HTTP 500');
  });
});

describe('ApiError constructor', () => {
  it('extracts envelope code and details onto the error instance', () => {
    const body = JSON.stringify({
      error: {
        code: 'max_tokens_out_of_range',
        message: 'knowledge_max_tokens must be between 0 and 20000. Got 25000.',
        details: { field: 'knowledge_max_tokens', max: 20000, current: 25000 },
      },
    });
    const err = new ApiError(422, body);
    expect(err.status).toBe(422);
    expect(err.code).toBe('max_tokens_out_of_range');
    expect(err.message).toContain('max_tokens_out_of_range');
    expect(err.message).toContain('knowledge_max_tokens');
    expect(err.details).toEqual({
      field: 'knowledge_max_tokens',
      max: 20000,
      current: 25000,
    });
    // Raw response body is preserved for power-user diagnostics.
    expect(err.raw).toBe(body);
  });

  it('preserves the plain-text body when no envelope is present', () => {
    const err = new ApiError(500, 'Internal Server Error');
    expect(err.status).toBe(500);
    expect(err.message).toBe('Internal Server Error');
    expect(err.code).toBeUndefined();
    expect(err.details).toBeUndefined();
    expect(err.raw).toBe('Internal Server Error');
  });
});
