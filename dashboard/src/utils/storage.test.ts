import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { getItem, setItem, removeItem } from './storage';

describe('storage utility', () => {
  // Snapshot the original localStorage descriptor so each test can restore it.
  let originalDescriptor: PropertyDescriptor | undefined;

  beforeEach(() => {
    originalDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage');
  });

  afterEach(() => {
    if (originalDescriptor) {
      Object.defineProperty(window, 'localStorage', originalDescriptor);
    }
  });

  it('reads and writes via the shared stub when available', () => {
    expect(setItem('k1', 'v1')).toBe(true);
    expect(getItem('k1')).toBe('v1');
    expect(removeItem('k1')).toBe(true);
    expect(getItem('k1')).toBeNull();
  });

  it('returns null / false when localStorage is missing entirely', () => {
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: undefined,
    });
    expect(getItem('k')).toBeNull();
    expect(setItem('k', 'v')).toBe(false);
    expect(removeItem('k')).toBe(false);
  });

  it('returns null / false when localStorage is a plain object without methods', () => {
    // Emulates the jsdom+vitest4 quirk that motivated the helper.
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {} as Storage,
    });
    expect(getItem('k')).toBeNull();
    expect(setItem('k', 'v')).toBe(false);
    expect(removeItem('k')).toBe(false);
  });

  it('swallows exceptions thrown by Storage methods (e.g. QuotaExceededError)', () => {
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: () => { throw new Error('SecurityError'); },
        setItem: () => { throw new Error('QuotaExceededError'); },
        removeItem: () => { throw new Error('SecurityError'); },
        clear: () => {},
        key: () => null,
        length: 0,
      },
    });
    expect(getItem('k')).toBeNull();
    expect(setItem('k', 'v')).toBe(false);
    expect(removeItem('k')).toBe(false);
  });

  it('swallows exceptions thrown by accessing localStorage itself', () => {
    // Some browsers throw SecurityError on the getter itself.
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() { throw new Error('SecurityError'); },
    });
    expect(getItem('k')).toBeNull();
    expect(setItem('k', 'v')).toBe(false);
    expect(removeItem('k')).toBe(false);
  });
});
