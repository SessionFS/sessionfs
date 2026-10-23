import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from 'react';
import { Markdown } from '../components/ui';

interface Heading {
  id: string;
  text: string;
  level: number;
}

// Below this many sections a table of contents is just noise.
const MIN_TOC_HEADINGS = 3;
const ID_PREFIX = 'ctx';

/**
 * The compiled project context, laid out for reading: the document at a
 * comfortable line length, with a table of contents of its sections beside it
 * on wide screens (sticky, highlighting the section being read) and as a
 * collapsible list above it on narrow ones. Context documents run to
 * thousands of words, so finding a section matters more than for a short page.
 */
export default function ContextDocument({ markdown }: { markdown: string }) {
  const articleRef = useRef<HTMLDivElement>(null);
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);

  // Read the headings back from the rendered document so the links use
  // exactly the ids the renderer assigned.
  useLayoutEffect(() => {
    const el = articleRef.current;
    if (!el) return;
    const found = Array.from(el.querySelectorAll<HTMLElement>('h1[id], h2[id], h3[id]')).map((h) => ({
      id: h.id,
      text: h.textContent?.trim() ?? '',
      level: Number(h.tagName[1]),
    }));
    // A lone document title ("# Project Context") adds nothing to the list.
    const titles = found.filter((h) => h.level === 1);
    setHeadings(titles.length === 1 ? found.filter((h) => h.level !== 1) : found);
  }, [markdown]);

  useEffect(() => {
    const el = articleRef.current;
    if (!el || headings.length < MIN_TOC_HEADINGS || typeof IntersectionObserver === 'undefined') return;
    // A heading becomes current once it scrolls into the top quarter of the
    // viewport and stays current until the next one does.
    const observer = new IntersectionObserver(
      (entries) => {
        const hit = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (hit) setActiveId(hit.target.id);
      },
      { rootMargin: '0px 0px -75% 0px' },
    );
    headings.forEach((h) => {
      const target = document.getElementById(h.id);
      if (target) observer.observe(target);
    });
    return () => observer.disconnect();
  }, [headings]);

  function jump(e: MouseEvent<HTMLAnchorElement>, id: string) {
    const target = document.getElementById(id);
    if (!target) return;
    e.preventDefault();
    target.setAttribute('tabindex', '-1');
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    target.focus({ preventScroll: true });
    setActiveId(id);
  }

  const showToc = headings.length >= MIN_TOC_HEADINGS;
  const minLevel = Math.min(...headings.map((h) => h.level));

  const tocList = (
    <ol className="space-y-0.5 text-sm">
      {headings.map((h) => (
        <li key={h.id}>
          <a
            href={`#${h.id}`}
            onClick={(e) => jump(e, h.id)}
            aria-current={activeId === h.id ? 'location' : undefined}
            className={`block rounded-md py-1 pr-2 border-l-2 transition-colors line-clamp-2 ${
              h.level > minLevel ? 'pl-6 text-xs' : 'pl-3'
            } ${
              activeId === h.id
                ? 'border-brand text-text-primary bg-surface-hover'
                : 'border-transparent text-text-tertiary hover:text-text-primary hover:bg-surface-hover'
            }`}
            title={h.text}
          >
            {h.text}
          </a>
        </li>
      ))}
    </ol>
  );

  return (
    <div className={showToc ? 'lg:grid lg:grid-cols-[minmax(0,1fr)_15rem] lg:gap-10' : ''}>
      <div className="min-w-0">
        {showToc && (
          <details className="lg:hidden mb-5 rounded-lg border border-border bg-bg-sunken">
            <summary className="cursor-pointer select-none px-4 py-2.5 text-sm font-medium text-text-primary">
              Sections ({headings.length})
            </summary>
            <nav aria-label="Context sections" className="px-2 pb-3">{tocList}</nav>
          </details>
        )}
        <div ref={articleRef} className="max-w-[78ch]">
          <Markdown headingIdPrefix={ID_PREFIX}>{markdown}</Markdown>
        </div>
      </div>
      {showToc && (
        <aside className="hidden lg:block">
          <nav aria-label="Context sections" className="sticky top-6 max-h-[calc(100vh-3rem)] overflow-y-auto">
            <p className="px-3 mb-2 text-2xs font-semibold uppercase tracking-wider text-text-tertiary">On this page</p>
            {tocList}
          </nav>
        </aside>
      )}
    </div>
  );
}
