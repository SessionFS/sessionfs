import type { CSSProperties } from 'react';
import type { LucideIcon } from 'lucide-react';

/* ── Icon ──
 *
 * The one way the dashboard draws an icon. Every icon is a Lucide glyph
 * (imported by name, so only the glyphs we use are bundled) drawn with a
 * 1.25 px stroke at every size (absoluteStrokeWidth), in currentColor, so it
 * follows the text colour of its role.
 *
 * Sizes: 14 next to 12-13 px text, 16 for buttons, menus and table actions
 * (the default), 20 for page headers and empty states. Icons are decorative
 * (aria-hidden) unless given a `label`; an icon-only button names itself with
 * its own aria-label, not the icon's. */

export type IconSize = 14 | 16 | 20;

interface IconProps {
  icon: LucideIcon;
  size?: IconSize;
  className?: string;
  style?: CSSProperties;
  /** Fill the shape with currentColor (an "on" bookmark, the primary star). */
  filled?: boolean;
  /** Accessible name. Without it the icon is hidden from assistive tech. */
  label?: string;
}

export function Icon({ icon: Glyph, size = 16, className, style, filled = false, label }: IconProps) {
  const a11y = label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': true as const };
  return (
    <Glyph
      size={size}
      strokeWidth={1.25}
      absoluteStrokeWidth
      color="currentColor"
      fill={filled ? 'currentColor' : 'none'}
      className={className}
      style={style}
      focusable="false"
      {...a11y}
    />
  );
}
