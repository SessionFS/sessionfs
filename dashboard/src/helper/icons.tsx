/** Icons used across the helper drawer: Lucide glyphs through the shared Icon. */

import {
  ArrowDown, ArrowLeft, ArrowUp, Check, ChevronDown, ChevronRight, CircleAlert, CircleCheck, CircleX, Copy, Folder,
  Folders, History, Key, Layers, Lock, Maximize2, MessageSquare, Minimize2, Pencil, RotateCcw, Sparkles, SquarePen,
  Trash2, X, type LucideIcon,
} from 'lucide-react';
import { Icon, type IconSize } from '../components/ui/Icon';

type P = { size?: IconSize; className?: string };

const make = (glyph: LucideIcon) => {
  const C = ({ size = 16, className = '' }: P) => <Icon icon={glyph} size={size} className={`shrink-0 ${className}`.trim()} />;
  return C;
};

export const SparkIcon = make(Sparkles);
/** Arrows pointing out: make the panel larger. */
export const ExpandIcon = make(Maximize2);
/** Arrows pointing in: back to the small panel. */
export const ShrinkIcon = make(Minimize2);
export const HistoryIcon = make(History);
export const NewChatIcon = make(SquarePen);
export const CloseIcon = make(X);
export const ChevronDownIcon = make(ChevronDown);
export const ChevronRightIcon = make(ChevronRight);
export const ArrowLeftIcon = make(ArrowLeft);
export const ArrowUpIcon = make(ArrowUp);
export const ArrowDownIcon = make(ArrowDown);

export const StopIcon = (p: { size?: number; className?: string }) => (
  <svg width={p.size ?? 12} height={p.size ?? 12} viewBox="0 0 24 24" aria-hidden="true" focusable="false" className={`shrink-0 ${p.className ?? ''}`}>
    <rect x="5" y="5" width="14" height="14" rx="2.5" fill="currentColor" />
  </svg>
);

export const FolderIcon = make(Folder);
export const FoldersIcon = make(Folders);
export const SessionIcon = make(MessageSquare);
export const LayersIcon = make(Layers);
export const CheckIcon = make(Check);
export const CheckCircleIcon = make(CircleCheck);
export const XCircleIcon = make(CircleX);
export const CopyIcon = make(Copy);
export const RetryIcon = make(RotateCcw);
export const TrashIcon = make(Trash2);
/** Rename (distinct from New conversation's square pen). */
export const RenameIcon = make(Pencil);
export const KeyIcon = make(Key);
export const LockIcon = make(Lock);
export const AlertIcon = make(CircleAlert);

export function Spinner({ size = 14, className = '' }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      focusable="false"
      className={`shrink-0 animate-spin ${className}`}
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
