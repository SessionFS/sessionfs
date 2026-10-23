import { Link } from 'react-router-dom';
import { Card } from '../../components/ui';

export function SessionListSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({length: 6}).map((_, i) => (
        <div key={i} className="bg-bg-elevated border border-border rounded-xl p-4 animate-pulse">
          <div className="flex items-center gap-3">
            <div className="w-3 h-3 rounded-full bg-bg-tertiary" />
            <div className="h-4 w-24 rounded bg-bg-tertiary" />
            <div className="h-3 w-32 rounded bg-bg-tertiary" />
            <div className="flex-1" />
            <div className="h-3 w-16 rounded bg-bg-tertiary" />
          </div>
          <div className="mt-2 flex items-center gap-3 pl-6">
            <div className="h-3 w-16 rounded bg-bg-tertiary" />
            <div className="h-3 w-64 rounded bg-bg-tertiary" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function NoSessionsCard() {
  return (
    <div className="flex items-center justify-center py-10">
      <Card
    level="elevated"
    className="max-w-[560px] w-full overflow-hidden"
      >
    {/* Dot-grid banner */}
    <div
      className="relative flex items-center justify-center py-10 bg-[image:var(--dot-grid)] bg-[length:16px_16px]"
    >
      {/* Session card illustration — two overlapping cards with a plus */}
      <svg
        width="88"
        height="72"
        viewBox="0 0 88 72"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        role="img"
        aria-label="Session cards illustration"
      >
        {/* Back card */}
        <rect
          x="4" y="2" width="56" height="60" rx="6"
          stroke="var(--text-tertiary)" strokeWidth="1.5"
          strokeDasharray="4 3"
        />
        {/* Front card */}
        <rect
          x="18" y="10" width="56" height="60" rx="6"
          stroke="var(--text-secondary)" strokeWidth="1.5"
        />
        {/* Plus circle */}
        <circle
          cx="64" cy="26" r="10"
          stroke="var(--brand)" strokeWidth="1.5"
        />
        <line
          x1="64" y1="21" x2="64" y2="31"
          stroke="var(--brand)" strokeWidth="1.5" strokeLinecap="round"
        />
        <line
          x1="59" y1="26" x2="69" y2="26"
          stroke="var(--brand)" strokeWidth="1.5" strokeLinecap="round"
        />
        {/* Accent line on front card */}
        <line
          x1="31" y1="28" x2="52" y2="28"
          stroke="var(--text-tertiary)" strokeWidth="1" strokeLinecap="round"
        />
        <line
          x1="31" y1="36" x2="44" y2="36"
          stroke="var(--text-tertiary)" strokeWidth="1" strokeLinecap="round"
          opacity="0.6"
        />
      </svg>
    </div>
    {/* Content area */}
    <div className="text-center px-6 pb-7 pt-5">
      <h2 className="text-title text-text-primary mb-1.5">
        No sessions yet
      </h2>
      <p className="text-caption max-w-sm mx-auto mb-5 leading-relaxed">
        SessionFS captures sessions automatically from your AI tools.
        Get started by installing the MCP server for your tool.
      </p>
      <div className="flex items-center justify-center gap-3">
        <Link
          to="/getting-started"
          className="px-4 py-2 text-sm font-semibold bg-brand text-white rounded-lg hover:bg-brand-hover transition-colors"
        >
          Get Started
        </Link>
        <Link
          to="/help"
          className="px-4 py-2 text-sm font-medium text-text-secondary border border-border rounded-lg hover:text-text-primary transition-colors"
        >
          View Help
        </Link>
      </div>
    </div>
      </Card>
    </div>
  );
}
