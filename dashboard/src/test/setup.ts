import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, expect, vi } from 'vitest';

// A control can be unavailable two ways: natively `disabled`, or
// `aria-disabled="true"` when it carries a reason (Button/IconButton
// `disabledReason`) and so stays focusable for its tooltip.
expect.extend({
  toBeUnavailable(received: Element) {
    const native = received instanceof HTMLElement && received.matches(':disabled');
    const aria = received.getAttribute('aria-disabled') === 'true';
    const pass = native || aria;
    return {
      pass,
      message: () =>
        pass
          ? `expected element to be available, but it is ${native ? 'disabled' : 'aria-disabled'}`
          : 'expected element to be disabled or aria-disabled="true"',
    };
  },
});

declare module 'vitest' {
  interface Assertion {
    toBeUnavailable(): void;
  }
  interface AsymmetricMatchersContaining {
    toBeUnavailable(): void;
  }
}

afterEach(() => {
  cleanup();
});

// vitest 4 + jsdom exposes `window.localStorage` as a plain object without the
// Storage interface, which breaks any code that calls `.getItem()`/`.setItem()`.
// Install a minimal in-memory implementation once up front, and reset it between
// tests so state doesn't leak across describes.
const lsBackingStore = new Map<string, string>();
const lsStub = {
  getItem: (k: string) => (lsBackingStore.has(k) ? lsBackingStore.get(k)! : null),
  setItem: (k: string, v: string) => { lsBackingStore.set(k, String(v)); },
  removeItem: (k: string) => { lsBackingStore.delete(k); },
  clear: () => { lsBackingStore.clear(); },
  key: (i: number) => Array.from(lsBackingStore.keys())[i] ?? null,
  get length() { return lsBackingStore.size; },
};

beforeAll(() => {
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: lsStub,
  });

  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });

  Object.defineProperty(window, 'scrollTo', {
    writable: true,
    value: vi.fn(),
  });

  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    writable: true,
    value: vi.fn(),
  });
});

beforeEach(() => {
  lsBackingStore.clear();
  document.documentElement.removeAttribute('data-theme');
});
