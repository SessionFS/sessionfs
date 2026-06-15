/**
 * Folder color palette — persisted user-chosen bookmark-folder colors.
 *
 * These are intentionally hardcoded hex (not design tokens): a folder's color
 * is user data stored on the server and rendered as-is, independent of theme.
 * Centralized here so the color picker (BookmarkSidebar) and the folder list
 * (SessionList) share one source of truth instead of duplicating the array.
 */
export const FOLDER_COLORS = [
  '#4f9cf7', '#3ddc84', '#bc8cff', '#f0a040',
  '#f04060', '#40c4f0', '#f0c040', '#f06090',
];

/** Default color when a folder has none stored. */
export const DEFAULT_FOLDER_COLOR = FOLDER_COLORS[0];
