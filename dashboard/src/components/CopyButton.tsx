import { useState } from 'react';

export default function CopyButton({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  return (
    <button
      onClick={handleCopy}
      className="inline-flex items-center gap-1 px-2 py-1 text-sm bg-bg-tertiary border border-border rounded hover:border-text-muted transition-colors"
      title={`Copy: ${text}`}
    >
      {copied ? (
        <span className="text-role-assistant">Copied</span>
      ) : (
        <span className="text-text-secondary">{label || 'Copy'}</span>
      )}
    </button>
  );
}
