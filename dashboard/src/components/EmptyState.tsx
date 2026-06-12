import type { ReactNode } from 'react';

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  actionLabel?: string;
  onAction?: () => void;
}

export default function EmptyState({ icon, title, description, actionLabel, onAction }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-4 text-center">
      {icon && (
        <div className="mb-4 text-text-tertiary">{icon}</div>
      )}
      <h3 className="text-xl font-semibold mb-1 text-text-primary">
        {title}
      </h3>
      {description && (
        <p className="text-md max-w-sm text-text-secondary">
          {description}
        </p>
      )}
      {actionLabel && onAction && (
        <button
          onClick={onAction}
          className="mt-4 px-5 py-2.5 text-sm font-semibold rounded-lg transition-colors"
          style={{
            backgroundColor: 'var(--brand)',
            color: 'var(--text-inverse)',
          }}
          onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = 'var(--brand-hover)')}
          onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'var(--brand)')}
        >
          {actionLabel}
        </button>
      )}
    </div>
  );
}
