import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { ProjectGroupDetail } from '../../api/client';
import { Card } from '../../components/ui';
import MeetingsTab from '../../meetings/MeetingsTab';

interface GroupMeetingsSectionProps {
  group: ProjectGroupDetail;
  /** Group admin, or an org admin/owner: may delete any meeting here. */
  canAdmin: boolean;
  className?: string;
}

/**
 * Meetings that belong to a project group (GitHub #116): a program's
 * cross-project meetings, whose action items become tickets in any group
 * project the caller can edit. Readers see everything read-only; editors and
 * admins add, extract, match and promote. The open meeting is in the URL
 * (`?meeting=&lines=`) like the project page.
 */
export default function GroupMeetingsSection({ group, canAdmin, className = '' }: GroupMeetingsSectionProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const canEdit = group.my_role === 'editor' || group.my_role === 'admin';
  const openMeeting = useCallback(
    (meetingId: string | null) => {
      setSearchParams((prev) => {
        const params = new URLSearchParams(prev);
        if (meetingId) params.set('meeting', meetingId);
        else params.delete('meeting');
        params.delete('lines');
        return params;
      });
    },
    [setSearchParams],
  );

  const setLines = useCallback(
    (lines: string | null) => {
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (lines) params.set('lines', lines);
          else params.delete('lines');
          return params;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  return (
    <Card level="elevated" className={`rounded-xl ${className}`}>
      <div className="p-5">
        <MeetingsTab
          scope={{ kind: 'group', id: group.id }}
          orgId={group.org_id}
          scopeLabel={group.name}
          canEdit={canEdit}
          canAdmin={canAdmin}
          meetingId={searchParams.get('meeting')}
          lines={searchParams.get('lines')}
          onOpenMeeting={openMeeting}
          onLinesChange={setLines}
          headingId="group-meetings-heading"
        />
      </div>
    </Card>
  );
}
