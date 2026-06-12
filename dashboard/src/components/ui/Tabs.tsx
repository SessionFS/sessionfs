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
      <div className="flex gap-1 border-b border-border pb-0" role="tablist">
        {tabs.map((tab) => {
          const active = tab.key === activeKey;
          return (
            <button
              key={tab.key}
              role="tab"
              aria-selected={active}
              onClick={() => onChange(tab.key)}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors cursor-pointer outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
                active
                  ? 'bg-surface border border-border text-text-primary'
                  : 'border border-transparent text-text-secondary hover:text-text-primary hover:bg-surface-hover'
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
