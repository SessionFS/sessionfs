export const AVATAR_COLORS = [
  'bg-info/20 text-info',
  'bg-success/20 text-success',
  'bg-role-discovery/20 text-role-discovery',
  'bg-warning/20 text-warning',
  'bg-danger/20 text-danger',
  'bg-brand/20 text-brand',
];

export function getAvatarColor(email: string): string {
  let hash = 0;
  for (let i = 0; i < email.length; i++) {
    hash = email.charCodeAt(i) + ((hash << 5) - hash);
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}
