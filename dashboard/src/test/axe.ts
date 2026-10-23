import { expect } from 'vitest';
import { configureAxe, toHaveNoViolations } from 'jest-axe';

expect.extend(toHaveNoViolations);

declare module 'vitest' {
  interface Assertion {
    toHaveNoViolations(): void;
  }
  interface AsymmetricMatchersContaining {
    toHaveNoViolations(): void;
  }
}

/**
 * axe configured for jsdom. Colour contrast needs real layout and computed
 * styles, which jsdom does not provide, so that rule is left to the
 * Playwright harness; `region` is meaningless for a fragment rendered
 * without the full page landmarks.
 */
export const axe = configureAxe({
  rules: {
    'color-contrast': { enabled: false },
    region: { enabled: false },
  },
});
