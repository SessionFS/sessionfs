/** Small stroke icons for the marketplace (24-unit viewBox, currentColor). */

import type { ReactNode } from 'react';

function Icon({ size = 16, className = '', children }: { size?: number; className?: string; children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={`shrink-0 ${className}`}
    >
      {children}
    </svg>
  );
}

type P = { size?: number; className?: string };

/** Filled-style check badge for a verified publisher. */
export const VerifiedIcon = (p: P) => (
  <Icon {...p}>
    <path d="M12 2l2.4 1.8 3-.2.9 2.9 2.5 1.7-1 2.8 1 2.8-2.5 1.7-.9 2.9-3-.2L12 22l-2.4-1.8-3 .2-.9-2.9-2.5-1.7 1-2.8-1-2.8 2.5-1.7.9-2.9 3 .2z" />
    <path d="M8.5 12.2l2.3 2.3 4.7-4.8" />
  </Icon>
);

export const ShieldCheckIcon = (p: P) => (
  <Icon {...p}>
    <path d="M12 3l7 3v5c0 4.4-3 8.3-7 10-4-1.7-7-5.6-7-10V6z" />
    <path d="M9 12l2 2 4-4" />
  </Icon>
);

export const AlertIcon = (p: P) => (
  <Icon {...p}>
    <path d="M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z" />
    <path d="M12 9v4" />
    <path d="M12 17h.01" />
  </Icon>
);

export const FilterIcon = (p: P) => (
  <Icon {...p}>
    <path d="M4 6h16" />
    <path d="M7 12h10" />
    <path d="M10 18h4" />
  </Icon>
);

export const SearchIcon = (p: P) => (
  <Icon {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="M20 20l-3.5-3.5" />
  </Icon>
);

export const CloseIcon = (p: P) => (
  <Icon {...p}>
    <path d="M18 6L6 18" />
    <path d="M6 6l12 12" />
  </Icon>
);

export const TerminalIcon = (p: P) => (
  <Icon {...p}>
    <path d="M4 17l6-5-6-5" />
    <path d="M12 19h8" />
  </Icon>
);

export const ChevronIcon = (p: P) => (
  <Icon {...p}>
    <path d="M9 6l6 6-6 6" />
  </Icon>
);
