import type { ReactNode } from 'react';

interface Tab {
  key: string;
  label: ReactNode;
  content?: ReactNode;
}

interface TabsProps {
  tabs: Tab[];
  activeKey: string;
  onChange: (key: string) => void;
  /** Render only the tab bar, no content panel. Use when content lives in a
   *  separate scrollable container (e.g. SessionDetail's flex-1 overflow). */
  bare?: boolean;
}

/**
 * Pill-style tabs matching the Phase 1b nav pattern:
 * active tab = --surface pill + hairline border, inactive = transparent.
 */
export function Tabs({ tabs, activeKey, onChange, bare = false }: TabsProps) {
  return (
    <div>
      <div className="flex gap-1 border-b border-[var(--border)] pb-0" role="tablist">
        {tabs.map((tab) => {
          const active = tab.key === activeKey;
          return (
            <button
              key={tab.key}
              role="tab"
              aria-selected={active}
              onClick={() => onChange(tab.key)}
              className={`px-3 py-1.5 rounded-[var(--radius-md)] text-[13px] font-medium transition-colors cursor-pointer ${
                active
                  ? 'bg-[var(--surface)] border border-[var(--border)] text-[var(--text-primary)]'
                  : 'border border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-hover)]'
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {!bare && (
        <div className="pt-4" role="tabpanel">
          {tabs.find((t) => t.key === activeKey)?.content}
        </div>
      )}
    </div>
  );
}
