import {
  Bell, Check, ChevronDown, ChevronLeft, ChevronUp, Copy, CornerDownRight, ExternalLink, Eye, Lock, ShieldCheck, Terminal,
  TriangleAlert, User, X, type LucideIcon,
} from 'lucide-react';
import { Icon, type IconSize } from '../../../components/ui/Icon';

/* Line icons for the ticket pane. Decorative: callers give the control
 * its accessible name. */

const ICONS = {
  copy: Copy,
  up: ChevronUp,
  down: ChevronDown,
  bell: Bell,
  close: X,
  back: ChevronLeft,
  tree: CornerDownRight,
  external: ExternalLink,
  shield: ShieldCheck,
  warn: TriangleAlert,
  eye: Eye,
  lock: Lock,
  terminal: Terminal,
  check: Check,
  user: User,
  chevron: ChevronDown,
} satisfies Record<string, LucideIcon>;

export type PaneIconName = keyof typeof ICONS;

/** One of the pane's line icons, decorative. */
export function PaneIcon({ name, size }: { name: PaneIconName; size?: IconSize }) {
  return <Icon icon={ICONS[name]} size={size} />;
}
