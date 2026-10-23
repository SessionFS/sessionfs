export function VersionBadge({ version }: { version: number }) {
  return (
    <span className="inline-flex items-center rounded-full bg-brand px-2 py-0.5 text-xs font-semibold text-text-on-brand">
      v{version}
    </span>
  );
}
