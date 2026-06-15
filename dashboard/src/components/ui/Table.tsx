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
}

export function Table<T>({ columns, data, onRowClick, rowKey }: TableProps<T>) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-separate border-spacing-0">
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
          No data
        </div>
      )}
    </div>
  );
}
