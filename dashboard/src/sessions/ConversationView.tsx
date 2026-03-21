import { useState } from 'react';
import { useMessages } from '../hooks/useMessages';
import MessageBlock from './MessageBlock';

interface Props {
  sessionId: string;
}

export default function ConversationView({ sessionId }: Props) {
  const [page, setPage] = useState(1);
  const { data, isLoading, error } = useMessages(sessionId, 1, page * 50);

  if (error) {
    return (
      <div className="p-4 text-red-400 text-sm">
        Failed to load messages: {String(error)}
      </div>
    );
  }

  if (isLoading && !data) {
    return <div className="p-4 text-text-muted">Loading messages...</div>;
  }

  const messages = data?.messages || [];

  return (
    <div className="flex flex-col gap-3 p-4">
      {messages.length === 0 && (
        <p className="text-text-muted text-sm">No messages in this session.</p>
      )}
      {messages.map((msg, i) => (
        <MessageBlock key={i} message={msg} />
      ))}
      {data?.has_more && (
        <button
          onClick={() => setPage((p) => p + 1)}
          disabled={isLoading}
          className="self-center px-4 py-1.5 text-sm bg-bg-secondary border border-border rounded hover:border-text-muted transition-colors text-text-secondary"
        >
          {isLoading ? 'Loading...' : `Load more (${data.total - messages.length} remaining)`}
        </button>
      )}
    </div>
  );
}
