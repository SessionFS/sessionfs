import { useEffect } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { Alert, PageContainer, PageHeader } from '../components/ui';
import { useMeeting } from '../hooks/useMeetings';
import { formatLinesParam, meetingActionError, meetingScopeHref, parseLines } from './format';

/**
 * `/meetings/:meetingId[?lines=a-b]` — one link for a meeting wherever it
 * lives (helper citations, toasts, other surfaces). Looks the meeting up and
 * replaces itself with its project's or group's page, lines kept.
 */
export default function MeetingRedirect() {
  const { meetingId = '' } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const meeting = useMeeting(meetingId || undefined);
  const range = parseLines(params.get('lines'));
  const lines = range ? formatLinesParam(range) : null;
  const target = meeting.data ? meetingScopeHref(meeting.data, lines) : null;

  useEffect(() => {
    if (target) navigate(target, { replace: true });
  }, [target, navigate]);

  if (meeting.isLoading || target) {
    return <div className="py-12 text-center text-text-tertiary">Opening meeting…</div>;
  }

  const gone = !meeting.data && (!(meeting.error instanceof ApiError) || meeting.error.status === 404);
  return (
    <PageContainer>
      <PageHeader
        title="Meeting"
        breadcrumb={
          <Link to="/projects" className="hover:underline">
            ← Projects
          </Link>
        }
      />
      <Alert variant={gone ? 'info' : 'danger'} title={gone ? 'Meeting not found' : 'Couldn’t open this meeting'}>
        {gone
          ? 'It may have been deleted, or you don’t have access to it.'
          : meetingActionError(meeting.error)}
      </Alert>
    </PageContainer>
  );
}
