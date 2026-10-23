import type { ReactNode } from 'react';

interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  /** Optional column width (Tailwind width class). */
  width?: string;
  /** Column alignment — defaults to 'left'. Use 'right' for quantitative columns (counts, sizes, money). */
  align?: 'left' | 'right';
}

interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  /** Called with the row data on click. */
  onRowClick?: (row: T) => void;
  /** Extract a unique key per row for React keys. */
  rowKey: (row: T) => string;
  /** Minimum table width in px; below it the table scrolls inside its wrapper instead of the page. */
  minWidth?: number;
  /** Shown in place of the rows when data is empty. Defaults to "No data". */
  emptyState?: ReactNode;
}

interface TableShellProps {
  children: ReactNode;
  /** Minimum table width in px; below it the wrapper scrolls horizontally. */
  minWidth?: number;
  /** Draw a rounded border around the scroll container. */
  bordered?: boolean;
  /** Extra classes for the <table> element. */
  className?: string;
  /** Wrapper classes (e.g. spacing). */
  wrapperClassName?: string;
}

/**
 * Scroll container + <table> for callers that render their own thead/tbody
 * (expandable rows, bespoke cells). Keeps wide tables from overflowing the
 * page at narrow widths.
 */
export function TableShell({ children, minWidth, bordered = false, className = '', wrapperClassName = '' }: TableShellProps) {
  return (
    <div className={`overflow-x-auto ${bordered ? 'rounded-lg border border-border' : ''} ${wrapperClassName}`.trim()}>
      <table
        className={`w-full text-left text-sm ${className}`.trim()}
        style={minWidth ? { minWidth: `${minWidth}px` } : undefined}
      >
        {children}
      </table>
    </div>
  );
}

export function Table<T>({ columns, data, onRowClick, rowKey, minWidth, emptyState }: TableProps<T>) {
  return (
    <div className="overflow-x-auto">
      <table
        className="w-full border-separate border-spacing-0"
        style={minWidth ? { minWidth: `${minWidth}px` } : undefined}
      >
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={`sticky top-0 z-10 bg-surface px-3 py-2 text-2xs font-semibold uppercase tracking-[0.04em] text-text-tertiary border-b border-border ${
                  col.align === 'right' ? 'text-right' : 'text-left'
                } ${col.width ?? ''}`}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={() => onRowClick?.(row)}
              className={`border-b border-border transition-colors duration-150 outline-none ${
                onRowClick ? 'cursor-pointer hover:bg-surface-hover focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] focus-visible:bg-surface-hover' : ''
              }`}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className={`px-3 py-2.5 text-sm text-text-primary ${
                    col.align === 'right' ? 'text-right tabular-nums' : 'text-left'
                  }`}
                >
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length === 0 && (
        <div className="px-3 py-8 text-center text-sm text-text-tertiary">
          {emptyState ?? 'No data'}
        </div>
      )}
    </div>
  );
}
