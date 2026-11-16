/** Icons used across the marketplace: Lucide glyphs through the shared Icon. */

import { BadgeCheck, ChevronRight, ListFilter, Search, ShieldCheck, Terminal, TriangleAlert, X, type LucideIcon } from 'lucide-react';
import { Icon, type IconSize } from '../components/ui/Icon';

type P = { size?: IconSize; className?: string };

const make = (glyph: LucideIcon) => {
  const C = ({ size = 16, className = '' }: P) => <Icon icon={glyph} size={size} className={`shrink-0 ${className}`.trim()} />;
  return C;
};

/** Verified publisher. */
export const VerifiedIcon = make(BadgeCheck);
export const ShieldCheckIcon = make(ShieldCheck);
export const AlertIcon = make(TriangleAlert);
export const FilterIcon = make(ListFilter);
export const SearchIcon = make(Search);
export const CloseIcon = make(X);
export const TerminalIcon = make(Terminal);
export const ChevronIcon = make(ChevronRight);
