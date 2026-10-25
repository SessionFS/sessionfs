import { isValidElement, type ReactNode } from 'react';
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import CopyButton from '../CopyButton';

// Minimal hast shapes for the heading-id plugin, so the dashboard doesn't
// depend on @types/hast directly.
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

function slugify(text: string): string {
  return (
    text
      .toLowerCase()
      .normalize('NFKD')
      .replace(/[̀-ͯ]/g, '')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60) || 'section'
  );
}

function textOf(node: HastNode): string {
  if (node.type === 'text') return node.value ?? '';
  return (node.children ?? []).map(textOf).join('');
}

/** Gives h1–h4 stable, unique ids (`<prefix>-<slug>`, `-2`, `-3` for
 *  repeats) so a table of contents can link to them. Done on the tree, not in
 *  the heading components, so the numbering can't drift between renders. */
function rehypeHeadingIds(prefix: string) {
  return () => (tree: HastNode) => {
    const seen = new Map<string, number>();
    const walk = (node: HastNode) => {
      if (node.type === 'element' && node.tagName && /^h[1-4]$/.test(node.tagName)) {
        const base = `${prefix}-${slugify(textOf(node))}`;
        const n = (seen.get(base) ?? 0) + 1;
        seen.set(base, n);
        node.properties = { ...node.properties, id: n === 1 ? base : `${base}-${n}` };
      }
      node.children?.forEach(walk);
    };
    walk(tree);
  };
}

/** Links keep only web, mail, in-page and same-site targets; anything else
 *  (javascript:, data:, custom schemes) renders as plain text. */
function safeHref(href: string | undefined): string | null {
  if (!href) return null;
  if (href.startsWith('#')) return href;
  if (href.startsWith('/')) {
    // Browsers read `/\host`, `//host` and `/<tab>/host` as another origin,
    // so a root-relative link must resolve to this origin to count as one.
    try {
      const origin = window.location.origin;
      return new URL(href, origin).origin === origin ? href : null;
    } catch {
      return null;
    }
  }
  try {
    const url = new URL(href);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? href : null;
  } catch {
    return null;
  }
}

/** react-markdown's default sanitizer drops `data:` URLs, but inline
 *  `data:image/…` srcs are safe to render (no network request). Allow exactly
 *  those through and delegate everything else to the default sanitizer. */
function urlTransform(url: string): string {
  return url.startsWith('data:image/') ? url : defaultUrlTransform(url);
}

// Long ids, URLs and paths are common in project documents; let them wrap
// instead of pushing the column wider than the screen.
const WRAP = '[overflow-wrap:anywhere]';
// A light tint of the text colour: reads as a well in both themes without
// the heavy near-black blocks a sunken background gives in dark mode.
const TINT = 'bg-[color-mix(in_oklch,var(--text-primary)_7%,transparent)]';

// index.css styles bare h1/h2 elements outside any layer, which beats
// utilities, so the heading sizes here are marked important.
const components: Components = {
  h1: ({ node, children, ...props }) => (
    <h1 className="scroll-mt-6 !text-[1.25rem] !leading-snug !font-semibold !tracking-tight text-text-primary mt-10 mb-3 first:mt-0" {...props}>{children}</h1>
  ),
  h2: ({ node, children, ...props }) => (
    <h2
      className="scroll-mt-6 !text-lg !leading-snug !font-semibold !tracking-normal text-text-primary mt-10 mb-3 pt-6 border-t border-border first:mt-0 first:pt-0 first:border-t-0"
      {...props}
    >
      {children}
    </h2>
  ),
  h3: ({ node, children, ...props }) => (
    <h3 className="scroll-mt-6 !text-md !font-semibold text-text-primary mt-7 mb-2" {...props}>{children}</h3>
  ),
  h4: ({ node, children, ...props }) => (
    <h4 className="scroll-mt-6 text-base font-semibold text-text-primary mt-5 mb-1.5" {...props}>{children}</h4>
  ),
  p: ({ node, children, ...props }) => (
    <p className={`my-3 ${WRAP}`} {...props}>{children}</p>
  ),
  strong: ({ node, children, ...props }) => (
    <strong className="font-semibold text-text-primary" {...props}>{children}</strong>
  ),
  a: ({ node, children, href }) => {
    void node;
    const safe = safeHref(href);
    if (!safe) return <span className={WRAP}>{children}</span>;
    const external = /^(https?:|mailto:)/.test(safe);
    return (
      <a
        href={safe}
        className={`text-brand underline underline-offset-2 decoration-brand/40 hover:decoration-brand ${WRAP}`}
        {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
      >
        {children}
      </a>
    );
  },
  // Remote images must never be loaded: a markdown image to another origin
  // could exfiltrate secrets via the URL query, so it renders as a plain link
  // instead. Only same-origin (`/…`) and inline data:image srcs load as <img>.
  img: ({ node, src, alt, ...props }) => {
    void node;
    const altText = typeof alt === 'string' ? alt : '';
    const local = src != null && (src.startsWith('data:image/') || (src.startsWith('/') && safeHref(src) !== null));
    if (local) {
      return <img src={src} alt={altText} className="max-w-full rounded-md my-2" {...props} />;
    }
    const safe = safeHref(src);
    if (safe) {
      const external = /^(https?:|mailto:)/.test(safe);
      return (
        <a
          href={safe}
          className={`text-brand underline underline-offset-2 decoration-brand/40 hover:decoration-brand ${WRAP}`}
          {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
        >
          {altText || safe}
        </a>
      );
    }
    return <span className={WRAP}>{altText || '[image]'}</span>;
  },
  ul: ({ node, children, ...props }) => (
    <ul className="list-disc pl-5 my-3 space-y-1.5 marker:text-text-tertiary" {...props}>{children}</ul>
  ),
  ol: ({ node, children, ...props }) => (
    <ol className="list-decimal pl-5 my-3 space-y-1.5 marker:text-text-tertiary" {...props}>{children}</ol>
  ),
  li: ({ node, children, ...props }) => (
    <li className={`pl-1 [&>p]:my-1 [&>ul]:my-1.5 [&>ol]:my-1.5 ${WRAP}`} {...props}>{children}</li>
  ),
  blockquote: ({ node, children, ...props }) => (
    <blockquote
      className={`my-4 border-l-2 border-brand/60 ${TINT} rounded-r-md px-4 py-1 text-text-secondary [&>p]:my-2`}
      {...props}
    >
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-8 border-border" />,
  code({ node, children, className, ...props }) {
    if (className?.startsWith('language-')) {
      return <code className={`font-mono ${className}`} {...props}>{children}</code>;
    }
    return (
      <code
        className={`font-mono text-[0.85em] ${TINT} rounded px-1 py-px text-text-primary ${WRAP}`}
        {...props}
      >
        {children}
      </code>
    );
  },
  pre({ node, children }) {
    void node;
    let lang = '';
    let text = '';
    if (isValidElement(children)) {
      const cp = children.props as { className?: string; children?: ReactNode };
      lang = cp.className?.replace('language-', '') ?? '';
      text = String(cp.children ?? '');
    }
    return (
      <div className="my-4 rounded-lg border border-code-well-border bg-code-well-bg overflow-hidden">
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-code-well-border">
          <span className="text-2xs font-mono text-code-well-muted">{lang || 'code'}</span>
          <CopyButton text={text.replace(/\n$/, '')} label="Copy code" />
        </div>
        <pre className="px-3 py-2.5 overflow-x-auto text-xs leading-relaxed font-mono text-code-well-fg">{children}</pre>
      </div>
    );
  },
  table: ({ node, children, ...props }) => (
    <div className="my-4 overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm border-collapse" {...props}>{children}</table>
    </div>
  ),
  thead: ({ node, children, ...props }) => (
    <thead className={`${TINT} text-text-primary`} {...props}>{children}</thead>
  ),
  th: ({ node, children, style, ...props }) => (
    <th className="px-3 py-2 text-left font-semibold border-b border-border" style={style} {...props}>{children}</th>
  ),
  td: ({ node, children, style, ...props }) => (
    <td className={`px-3 py-2 align-top border-t border-border ${WRAP}`} style={style} {...props}>{children}</td>
  ),
  input: ({ node, ...props }) => (
    // GFM task-list checkboxes: shown, never editable here.
    <input {...props} disabled className="mr-1.5 align-middle accent-[var(--brand)]" />
  ),
};

interface MarkdownProps {
  children: string;
  /** Prefix for heading ids. Set it to give headings anchors (for a table of
   *  contents); keep it distinct per document on a page. */
  headingIdPrefix?: string;
  className?: string;
}

/**
 * Markdown for documents people read in the dashboard: project context, wiki
 * pages. GitHub-flavoured (tables, task lists, strikethrough), single line
 * breaks kept as breaks (agents write one fact per line), raw HTML never
 * rendered, and links limited to safe targets.
 */
export function Markdown({ children, headingIdPrefix, className = '' }: MarkdownProps) {
  return (
    <div className={`text-base leading-7 text-text-secondary ${className}`}>
      <ReactMarkdown
        skipHtml
        urlTransform={urlTransform}
        remarkPlugins={[remarkGfm, remarkBreaks]}
        rehypePlugins={headingIdPrefix ? [rehypeHeadingIds(headingIdPrefix)] : []}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
