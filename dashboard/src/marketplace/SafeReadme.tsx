import { useEffect, useState } from 'react';
import Markdown, { type Components, defaultUrlTransform } from 'react-markdown';
import { useAuth } from '../auth/AuthContext';
import type { MarketplaceReadme } from '../api/client';
import { packageImagePath, safeLinkHref } from './discoveryUtils';

/**
 * Package README rendering (marketplace v2 §9.2). The text is publisher
 * controlled and untrusted:
 * - raw HTML is never rendered (`skipHtml`; no rehype-raw);
 * - links keep only http(s) / mailto targets and always carry
 *   rel="noopener noreferrer nofollow"; anything else renders as plain text;
 * - remote images are never loaded (tracking pixels): only a relative path
 *   inside the package is fetched, through the authenticated, same-origin
 *   asset endpoint, as a Blob shown via an object URL;
 * - hidden / bidirectional characters arrive already replaced by ⟦U+XXXX⟧
 *   markers and are listed above the text.
 */


function PackageImage({ versionId, path, alt }: { versionId: string; path: string; alt: string }) {
  const { auth } = useAuth();
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let revoked = false;
    let objectUrl: string | null = null;
    auth?.client
      .getMarketplaceAsset(versionId, path)
      .then((blob) => {
        if (revoked) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!revoked) setFailed(true);
      });
    return () => {
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [auth, versionId, path]);
  if (failed) return <span className="text-xs text-text-tertiary">[image unavailable: {alt || path}]</span>;
  if (!url) return <span className="text-xs text-text-tertiary">[loading image: {alt || path}]</span>;
  return <img src={url} alt={alt} className="my-2 max-h-96 max-w-full rounded border border-border" />;
}

function components(versionId: string): Components {
  return {
    a({ node, href, children }) {
      void node;
      const safe = safeLinkHref(href);
      if (!safe) return <span className="text-text-secondary">{children}</span>;
      return (
        <a href={safe} target="_blank" rel="noopener noreferrer nofollow" className="text-brand underline-offset-2 hover:underline">
          {children}
        </a>
      );
    },
    img({ node, src, alt }) {
      void node;
      const path = packageImagePath(typeof src === 'string' ? src : null);
      if (!path) {
        return <span className="text-xs text-text-tertiary">[remote image not loaded: {alt || 'image'}]</span>;
      }
      return <PackageImage versionId={versionId} path={path} alt={alt || ''} />;
    },
    h1: ({ node, children }) => (void node, <h1 className="mt-4 mb-2 text-lg font-semibold text-text-primary">{children}</h1>),
    h2: ({ node, children }) => (void node, <h2 className="mt-4 mb-1.5 text-md font-semibold text-text-primary">{children}</h2>),
    h3: ({ node, children }) => (void node, <h3 className="mt-3 mb-1 text-base font-semibold text-text-primary">{children}</h3>),
    p: ({ node, children }) => (void node, <p className="my-2 text-sm leading-6 text-text-secondary">{children}</p>),
    ul: ({ node, children }) => (void node, <ul className="my-2 list-disc pl-5 text-sm text-text-secondary">{children}</ul>),
    ol: ({ node, children }) => (void node, <ol className="my-2 list-decimal pl-5 text-sm text-text-secondary">{children}</ol>),
    code: ({ node, children }) => (void node, <code className="rounded bg-bg-sunken px-1 py-0.5 font-mono text-xs text-[var(--accent)]">{children}</code>),
    pre: ({ node, children }) => (void node, <pre className="my-3 overflow-x-auto whitespace-pre-wrap rounded-lg border border-border bg-bg-sunken p-3 font-mono text-xs text-text-secondary">{children}</pre>),
    blockquote: ({ node, children }) => (void node, <blockquote className="my-2 border-l-2 border-border pl-3 text-text-tertiary">{children}</blockquote>),
  };
}

/** Same scheme guard as react-markdown's default, applied to every URL. */
function urlTransform(url: string): string {
  return defaultUrlTransform(url);
}

export function SafeReadme({ readme, versionId }: { readme: MarketplaceReadme; versionId: string }) {
  if (readme.source === 'none' || !readme.content.trim()) {
    return <p className="text-sm text-text-tertiary">This package has no README or SKILL.md text to show.</p>;
  }
  return (
    <div>
      <p className="mb-2 text-2xs uppercase tracking-wider text-text-tertiary">
        {readme.source === 'readme' ? readme.path : `No README: showing ${readme.path}`} · publisher-supplied, untrusted
      </p>
      {readme.hidden_unicode.length > 0 && (
        <div role="alert" className="mb-3 rounded-lg border border-warning/40 bg-warning/6 p-3 text-xs text-warning">
          <p className="font-semibold">Hidden or bidirectional characters</p>
          <p className="mt-1 text-text-secondary">Shown below as ⟦U+XXXX⟧ markers:</p>
          <ul className="mt-1 font-mono">
            {readme.hidden_unicode.slice(0, 20).map((flag) => (
              <li key={`${flag.line}:${flag.column}`}>line {flag.line}, column {flag.column}: {flag.code_point} ({flag.kind})</li>
            ))}
            {readme.hidden_unicode.length > 20 && <li>…and {readme.hidden_unicode.length - 20} more</li>}
          </ul>
        </div>
      )}
      <div className="rounded-xl border border-border bg-bg-elevated p-4" data-testid="safe-readme">
        <Markdown skipHtml urlTransform={urlTransform} components={components(versionId)}>
          {readme.content}
        </Markdown>
      </div>
      {readme.truncated && <p className="mt-2 text-xs text-text-tertiary">The README was truncated at 256 KiB.</p>}
    </div>
  );
}
