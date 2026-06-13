type WordmarkSize = 'sm' | 'md' | 'lg';

interface WordmarkProps {
  size?: WordmarkSize;
  showTagline?: boolean;
  className?: string;
}

const SIZE_CLASSES: Record<WordmarkSize, string> = {
  sm: 'h-7',
  md: 'h-8',
  lg: 'h-10',
};

export default function Wordmark({
  size = 'md',
  showTagline = false,
  className = '',
}: WordmarkProps) {
  return (
    <div className={`flex items-center gap-3 ${className}`.trim()}>
      <svg
        viewBox="0 0 180 50"
        className={SIZE_CLASSES[size]}
        xmlns="http://www.w3.org/2000/svg"
        role="img"
        aria-label="SessionFS"
      >
        <path
          d="M22,8 Q14,8 14,16 L14,34 Q14,42 22,42"
          fill="none"
          stroke="var(--brand)"
          strokeWidth="3"
          strokeLinecap="round"
        />
        <path
          d="M42,8 Q50,8 50,16 L50,34 Q50,42 42,42"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="3"
          strokeLinecap="round"
        />
        <circle cx="32" cy="25" r="5" fill="var(--brand)" />
        <text
          x="60"
          y="32"
          fontFamily="var(--font-display)"
          fontSize="19"
          fontWeight="700"
          fill="var(--text-primary)"
          letterSpacing="-0.5"
        >
          Session
          <tspan fill="var(--brand)">FS</tspan>
        </text>
      </svg>
      {showTagline && (
        <span className="hidden xl:block text-2xs font-semibold uppercase tracking-[0.16em] text-text-tertiary">
          Memory Layer For AI Agents
        </span>
      )}
    </div>
  );
}
