import type { ReactNode, MouseEvent } from 'react';

interface CardProps {
  children: ReactNode;
  className?: string;
  onClick?: (e: MouseEvent<HTMLDivElement>) => void;
  hoverable?: boolean;
}

export default function Card({ children, className = '', onClick, hoverable = false }: CardProps) {
  return (
    <div
      onClick={onClick}
      className={[
        'rounded-[var(--radius-lg)] border border-[var(--border)]',
        'bg-[var(--bg-elevated)]',
        hoverable
          ? 'transition-[border-color] duration-150 hover:border-[var(--border-strong)] cursor-pointer outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]'
          : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {children}
    </div>
  );
}
