import type { HTMLAttributes, ReactNode } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  level?: 'surface' | 'elevated';
  /** CSS color value for a 3px left edge (e.g. a TOOL_COLORS entry). */
  toolEdge?: string;
  children: ReactNode;
}

export function Card({
  level = 'surface',
  toolEdge,
  children,
  className = '',
  style,
  ...rest
}: CardProps) {
  const bgVar = level === 'elevated' ? 'var(--bg-elevated)' : 'var(--surface)';

  return (
    <div
      className={`rounded-lg border border-[var(--border)] ${className}`}
      style={{
        backgroundColor: bgVar,
        ...(toolEdge
          ? { borderLeftWidth: '3px', borderLeftStyle: 'solid', borderLeftColor: toolEdge }
          : {}),
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}
