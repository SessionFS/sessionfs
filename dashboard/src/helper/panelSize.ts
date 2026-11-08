/**
 * The helper drawer's two sizes. "small" is the classic drawer; "large" is a
 * much wider reading panel for long answers, tables and code. The choice is
 * remembered per browser (localStorage, never throws). Phones always get the
 * full-screen drawer, so neither class changes anything below `md`.
 */

import { getItem, setItem } from '../utils/storage';

export type HelperPanelSize = 'small' | 'large';

export const PANEL_SIZE_KEY = 'sfs-helper-panel-size';

export function readPanelSize(): HelperPanelSize {
  return getItem(PANEL_SIZE_KEY) === 'large' ? 'large' : 'small';
}

export function writePanelSize(size: HelperPanelSize): void {
  setItem(PANEL_SIZE_KEY, size);
}

const MOTION = 'transition-[width] duration-200 ease-out motion-reduce:transition-none';

/**
 * Small: 480 px (520 px from `lg`). Large: 62 % of the viewport, at least
 * 600 px and at most 960 px, from `md` up: about 635 px at 1024, 794 px at
 * 1280, 893 px at 1440 and 960 px at 1920, so the page behind stays in view.
 */
export const PANEL_WIDTH: Record<HelperPanelSize, string> = {
  small: `w-full sm:w-[480px] lg:w-[520px] ${MOTION}`,
  large: `w-full sm:w-[480px] md:w-[min(960px,max(600px,62vw))] ${MOTION}`,
};
