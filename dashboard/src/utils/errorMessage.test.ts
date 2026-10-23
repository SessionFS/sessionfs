import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import { errorMessage } from './errorMessage';

describe('errorMessage', () => {
  it('strips the Error: prefix', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom');
    expect(errorMessage('Error: Error: boom')).toBe('boom');
    expect(errorMessage(new TypeError('bad'))).toBe('bad');
  });

  it('uses the ApiError envelope message without the code prefix', () => {
    const body = JSON.stringify({ error: { code: 'max_tokens_exceeded', message: 'Too long' } });
    expect(errorMessage(new ApiError(422, body))).toBe('Too long');
  });

  it('keeps the code when the envelope has no message', () => {
    const body = JSON.stringify({ error: { code: 'forbidden' } });
    expect(errorMessage(new ApiError(403, body))).toBe('forbidden');
  });

  it('handles legacy detail bodies and plain text', () => {
    expect(errorMessage(new ApiError(400, JSON.stringify({ detail: 'Nope' })))).toBe('Nope');
    expect(errorMessage(new ApiError(502, 'Bad gateway'))).toBe('Bad gateway');
  });

  it('accepts strings and message-bearing objects', () => {
    expect(errorMessage('plain')).toBe('plain');
    expect(errorMessage({ message: 'obj' })).toBe('obj');
  });

  it('falls back for unusable values', () => {
    expect(errorMessage(undefined)).toBe('Something went wrong');
    expect(errorMessage(null, 'Custom')).toBe('Custom');
    expect(errorMessage(new Error(''))).toBe('Something went wrong');
  });
});
