import { isValidElement, type ReactNode } from 'react';
import { type Components } from 'react-markdown';
import { CopyButton } from './CopyButton';


// `node` is the markdown AST node react-markdown injects; we destructure it
// out of every handler so it is never spread onto a DOM element (React would
// warn about an unrecognized `node` prop). Typed as react-markdown's
// `Components` so the per-element props are inferred — no explicit `any`.
export const markdownComponents: Components = {
  // Inline code — distinguished from fenced code blocks
  code({ node, children, className, ...props }) {
    if (className?.startsWith('language-')) {
      // Fenced code block — leave styling to the <pre> wrapper
      return (
        <code className={`font-mono text-xs ${className}`} {...props}>
          {children}
        </code>
      );
    }
    return (
      <code className="font-mono text-xs bg-bg-sunken text-[var(--accent)] px-1 py-0.5 rounded" {...props}>
        {children}
      </code>
    );
  },
  // Fenced code blocks — wrapped in a sunken well with language label + copy
  pre({ node, children, ...props }) {
    let lang = '';
    let codeContent = '';
    if (isValidElement(children)) {
      const cp = children.props as { className?: string; children?: ReactNode };
      if (cp.className?.startsWith('language-')) {
        lang = cp.className.replace('language-', '');
      }
      codeContent = String(cp.children ?? '');
    }
    return (
      <div className="bg-bg-sunken border border-border rounded-lg overflow-hidden my-3">
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-border">
          <span className="text-2xs font-mono text-text-tertiary">{lang || 'code'}</span>
          <CopyButton text={codeContent} label="Copy code" />
        </div>
        <pre className="px-3 py-2 overflow-x-auto text-xs font-mono text-text-secondary whitespace-pre-wrap" {...props}>
          {children}
        </pre>
      </div>
    );
  },
  h1: ({ node, children, ...props }) => (
    <h1 className="text-lg font-semibold text-text-primary mt-6 mb-2" {...props}>{children}</h1>
  ),
  h2: ({ node, children, ...props }) => (
    <h2 className="text-md font-semibold text-text-primary mt-5 mb-1.5" {...props}>{children}</h2>
  ),
  h3: ({ node, children, ...props }) => (
    <h3 className="text-base font-semibold text-text-primary mt-4 mb-1" {...props}>{children}</h3>
  ),
  h4: ({ node, children, ...props }) => (
    <h4 className="text-sm font-semibold text-text-primary mt-3 mb-1" {...props}>{children}</h4>
  ),
  p: ({ node, children, ...props }) => (
    <p className="text-sm text-text-secondary leading-relaxed my-2" {...props}>{children}</p>
  ),
  a: ({ node, children, href, ...props }) => (
    <a
      className="text-[var(--brand)] hover:text-[var(--brand-hover)] underline underline-offset-2 transition-colors duration-150"
      href={href}
      {...props}
    >
      {children}
    </a>
  ),
  ul: ({ node, children, ...props }) => (
    <ul className="list-disc pl-5 my-2 space-y-0.5 text-sm text-text-secondary" {...props}>{children}</ul>
  ),
  ol: ({ node, children, ...props }) => (
    <ol className="list-decimal pl-5 my-2 space-y-0.5 text-sm text-text-secondary" {...props}>{children}</ol>
  ),
  li: ({ node, children, ...props }) => (
    <li className="text-sm text-text-secondary" {...props}>{children}</li>
  ),
  blockquote: ({ node, children, ...props }) => (
    <blockquote className="border-l-2 border-border pl-3 my-2 text-text-tertiary italic" {...props}>{children}</blockquote>
  ),
  hr: ({ node, ...props }) => <hr className="border-border my-4" {...props} />,
  strong: ({ node, children, ...props }) => (
    <strong className="font-semibold text-text-primary" {...props}>{children}</strong>
  ),
  em: ({ node, children, ...props }) => (
    <em className="italic text-text-secondary" {...props}>{children}</em>
  ),
};
