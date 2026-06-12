import type React from 'react';

interface SkeletonProps {
  lines?: number;
  type?: 'card' | 'row' | 'text';
}

function SkeletonBlock({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return (
    <div
      className={`rounded-sm animate-skeleton-pulse ${className ?? ''}`}
      style={{ backgroundColor: 'var(--bg-tertiary)', ...style }}
    />
  );
}

export default function Skeleton({ lines = 3, type = 'text' }: SkeletonProps) {
  if (type === 'card') {
    return (
      <div className="rounded-lg border border-border bg-bg-elevated p-4 space-y-3">
        <SkeletonBlock className="h-4 w-2/3" />
        <SkeletonBlock className="h-3 w-full" />
        <SkeletonBlock className="h-3 w-4/5" />
      </div>
    );
  }

  if (type === 'row') {
    return (
      <div className="flex items-center gap-3 py-2">
        <SkeletonBlock className="h-8 w-8 rounded-full shrink-0" />
        <div className="flex-1 space-y-2">
          <SkeletonBlock className="h-3 w-1/3" />
          <SkeletonBlock className="h-3 w-2/3" />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {Array.from({ length: lines }).map((_, i) => (
        <SkeletonBlock
          key={i}
          className="h-3"
          style={{ width: i === lines - 1 ? '60%' : '100%' }}
        />
      ))}
    </div>
  );
}
