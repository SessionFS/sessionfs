import type { ReactNode } from 'react';

interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  /** Optional column width (Tailwind width class). */
  width?: string;
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
      <table className="w-full text-left">
        <thead>
          <tr className="border-b border-[var(--border)]">
            {columns.map((col) => (
              <th
                key={col.key}
                className={`px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.04em] text-[var(--text-tertiary)] ${col.width ?? ''}`}
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
              className={`border-b border-[var(--border)] transition-colors duration-150 ${
                onRowClick ? 'cursor-pointer hover:bg-[var(--surface-hover)]' : ''
              }`}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className="px-3 py-2.5 text-[13px] text-[var(--text-primary)]"
                >
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length === 0 && (
        <div className="px-3 py-8 text-center text-[13px] text-[var(--text-tertiary)]">
          No data
        </div>
      )}
    </div>
  );
}
