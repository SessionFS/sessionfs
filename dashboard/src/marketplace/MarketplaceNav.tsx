import { NavLink } from 'react-router-dom';
import { useMe } from '../hooks/useMe';
import { useMyOrgs } from '../transfers/useTransfers';
import { adminOrgs, canModerate, canPublishPersonal } from './moderationState';

/**
 * Shared sub-navigation for every /marketplace/* page. Each destination is
 * shown only to users who can use it: Publishers to anyone who can publish,
 * Usage to org owners/admins, Moderation to platform admins and org admins.
 */
export default function MarketplaceNav() {
  const me = useMe();
  const myOrgs = useMyOrgs();
  const isAdminOfOrg = adminOrgs(myOrgs.data, me.data).length > 0;
  const canPublish = canPublishPersonal(me.data) || isAdminOfOrg;
  const moderator = canModerate(me.data, myOrgs.data);

  const links = [
    { to: '/marketplace', label: 'Browse', end: true, show: true },
    { to: '/marketplace/publishers', label: 'Publishers', end: false, show: canPublish },
    { to: '/marketplace/usage', label: 'Usage', end: false, show: isAdminOfOrg },
    { to: '/marketplace/moderation', label: 'Moderation', end: false, show: moderator },
  ].filter((l) => l.show);

  return (
    <nav aria-label="Marketplace sections" className="mb-5 flex flex-wrap gap-1">
      {links.map((l) => (
        <NavLink
          key={l.to}
          to={l.to}
          end={l.end}
          className={({ isActive }) =>
            `rounded-md border px-3 py-1.5 text-sm font-medium transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
              isActive
                ? 'bg-surface border-border text-text-primary'
                : 'border-transparent text-text-secondary hover:text-text-primary hover:bg-surface-hover'
            }`
          }
        >
          {l.label}
        </NavLink>
      ))}
    </nav>
  );
}
