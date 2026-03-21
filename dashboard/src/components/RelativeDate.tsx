export default function RelativeDate({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="text-text-muted">-</span>;
  const d = new Date(iso);
  const now = Date.now();
  const sec = Math.floor((now - d.getTime()) / 1000);
  let text: string;
  if (sec < 60) text = `${sec}s ago`;
  else if (sec < 3600) text = `${Math.floor(sec / 60)}m ago`;
  else if (sec < 86400) text = `${Math.floor(sec / 3600)}h ago`;
  else if (sec < 604800) text = `${Math.floor(sec / 86400)}d ago`;
  else text = d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
  return <span title={d.toISOString()}>{text}</span>;
}
