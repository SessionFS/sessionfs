export function TerminalIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2.5" width="13" height="11" rx="2" />
      <path d="M4.5 5.5l2 2-2 2M8 9.5h3.5" />
    </svg>
  );
}

export function FileIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 1.5H4a1.5 1.5 0 0 0-1.5 1.5v10A1.5 1.5 0 0 0 4 14.5h8a1.5 1.5 0 0 0 1.5-1.5V6L9 1.5Z" />
      <path d="M9 1.5V6h4.5" />
    </svg>
  );
}

export function EditIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11.5 1.5l3 3-9.5 9.5L1.5 14.5l.5-3.5 9.5-9.5Z" />
      <path d="M10 3l3 3" />
    </svg>
  );
}

export function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.5 10.5L14.5 14.5" />
    </svg>
  );
}

export function GlobeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6.5" />
      <ellipse cx="8" cy="8" rx="3" ry="6.5" />
      <path d="M1.5 8h13M8 1.5v13" />
    </svg>
  );
}

export function TodoIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2" width="13" height="12" rx="1.5" />
      <path d="M4.5 6.5l2 2 4.5-4.5" />
    </svg>
  );
}

export function BotIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="5" width="10" height="8" rx="1.5" />
      <path d="M6 3.5V5M10 3.5V5" />
      <circle cx="8" cy="1.5" r="1" />
      <path d="M6 9h4M6 11h2.5" />
    </svg>
  );
}

export function DefaultToolIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12.5 3.5a4 4 0 0 0-5.66 5.34l-5.34 2.66 1 2 2.66-1.34a4 4 0 0 0 5.34-5.66l-2 2a.5.5 0 0 1-.7-.7l2-2Z" />
    </svg>
  );
}

export function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`transition-transform duration-150 ${open ? 'rotate-90' : 'rotate-0'}`}
    >
      <path d="M4.5 2.5L8 6l-3.5 3.5" />
    </svg>
  );
}

export function CopyIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="7" height="7" rx="1" />
      <path d="M2.5 8H2a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v.5" />
    </svg>
  );
}
