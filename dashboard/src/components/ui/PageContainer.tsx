import type { HTMLAttributes, ReactNode } from 'react';

type PageWidth = 'narrow' | 'default' | 'wide' | 'full';

interface PageContainerProps extends HTMLAttributes<HTMLDivElement> {
  /** narrow = 3xl, default = 5xl, wide = 7xl, full = no max width. */
  width?: PageWidth;
  children: ReactNode;
}

const WIDTHS: Record<PageWidth, string> = {
  narrow: 'max-w-3xl',
  default: 'max-w-5xl',
  wide: 'max-w-7xl',
  full: '',
};

/**
 * Standard page wrapper: centred, width preset, one gutter and vertical rhythm.
 * Renders a plain div on purpose: Layout already provides the page's single
 * <main> landmark, so nesting another would duplicate it.
 */
export function PageContainer({ width = 'default', children, className = '', ...rest }: PageContainerProps) {
  return (
    <div className={`${WIDTHS[width]} w-full mx-auto px-4 py-6 ${className}`.trim()} {...rest}>
      {children}
    </div>
  );
}
