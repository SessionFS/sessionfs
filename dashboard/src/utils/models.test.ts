import { describe, expect, it } from 'vitest';
import { friendlyModelName } from './models';

describe('friendlyModelName', () => {
  it('strips a trailing -YYYYMMDD date stamp', () => {
    expect(friendlyModelName('gpt-5-20250127')).toBe('gpt-5');
    expect(friendlyModelName('claude-sonnet-4-6-20250929')).toBe('claude-sonnet-4-6');
  });
  it('strips a trailing @YYYYMMDD date stamp', () => {
    expect(friendlyModelName('o3@20250929')).toBe('o3');
  });
  it('leaves ids without a date stamp untouched', () => {
    expect(friendlyModelName('claude-sonnet-4-6')).toBe('claude-sonnet-4-6');
    expect(friendlyModelName('gpt-4o')).toBe('gpt-4o');
  });
  it('returns empty for a null/empty id (callers label it "Unknown model")', () => {
    expect(friendlyModelName(null)).toBe('');
    expect(friendlyModelName(undefined)).toBe('');
    expect(friendlyModelName('')).toBe('');
  });
});
