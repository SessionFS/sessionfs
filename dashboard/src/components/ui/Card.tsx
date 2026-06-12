import type { HTMLAttributes, ReactNode } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  level?: 'surface' | 'elevated';
  /** CSS color value for a 3px left edge (e.g. a TOOL_COLORS entry). */
  toolEdge?: string;
  /** CSS color value for a 3px top edge (e.g. brand stripe for org-scoped projects). */
  topEdge?: string;
  children: ReactNode;
}

export function Card({
  level = 'surface',
  toolEdge,
  topEdge,
  children,
  className = '',
  style,
  ...rest
}: CardProps) {
  const bgVar = level === 'elevated' ? 'var(--bg-elevated)' : 'var(--surface)';

  return (
    <div
      className={`rounded-lg border border-border ${toolEdge || topEdge ? 'overflow-hidden' : ''} ${className}`}
      style={{
        backgroundColor: bgVar,
        ...(toolEdge
          ? { borderLeftWidth: '3px', borderLeftStyle: 'solid', borderLeftColor: toolEdge }
          : {}),
        ...(topEdge
          ? { borderTopWidth: '3px', borderTopStyle: 'solid', borderTopColor: topEdge }
          : {}),
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}
