import type { ReactNode } from 'react';

/* Small line icons for the ticket pane. Decorative: callers give the control
 * its accessible name. */

function Svg({ size = 16, children, className }: { size?: number; children: ReactNode; className?: string }) {
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
      className={className}
    >
      {children}
    </svg>
  );
}

const ICONS = {
  copy: (s?: number) => (
    <Svg size={s}>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </Svg>
  ),
  up: (s?: number) => (
    <Svg size={s}>
      <polyline points="6 15 12 9 18 15" />
    </Svg>
  ),
  down: (s?: number) => (
    <Svg size={s}>
      <polyline points="6 9 12 15 18 9" />
    </Svg>
  ),
  bell: (s?: number) => (
    <Svg size={s}>
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </Svg>
  ),
  close: (s?: number) => (
    <Svg size={s}>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </Svg>
  ),
  back: (s?: number) => (
    <Svg size={s}>
      <polyline points="15 18 9 12 15 6" />
    </Svg>
  ),
  tree: (s?: number) => (
    <Svg size={s}>
      <path d="M6 3v12a3 3 0 0 0 3 3h9" />
      <polyline points="15 15 18 18 15 21" />
    </Svg>
  ),
  external: (s?: number) => (
    <Svg size={s}>
      <path d="M15 3h6v6" />
      <path d="M10 14 21 3" />
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    </Svg>
  ),
  shield: (s?: number) => (
    <Svg size={s}>
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      <polyline points="9 12 11 14 15 10" />
    </Svg>
  ),
  warn: (s?: number) => (
    <Svg size={s}>
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </Svg>
  ),
  eye: (s?: number) => (
    <Svg size={s}>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </Svg>
  ),
  lock: (s?: number) => (
    <Svg size={s}>
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </Svg>
  ),
  terminal: (s?: number) => (
    <Svg size={s}>
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </Svg>
  ),
  check: (s?: number) => (
    <Svg size={s}>
      <polyline points="20 6 9 17 4 12" />
    </Svg>
  ),
  user: (s?: number) => (
    <Svg size={s}>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </Svg>
  ),
  chevron: (s?: number) => (
    <Svg size={s}>
      <polyline points="6 9 12 15 18 9" />
    </Svg>
  ),
};

export type PaneIconName = keyof typeof ICONS;

/** One of the pane's line icons, decorative. */
export function PaneIcon({ name, size }: { name: PaneIconName; size?: number }) {
  return ICONS[name](size);
}
