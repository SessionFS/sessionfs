import { createContext, useContext, useState, useCallback, useEffect, useRef, type ReactNode } from 'react';
import { useAuth } from '../auth/AuthContext';

interface BackgroundTask {
  id: string;
  type: 'audit';
  sessionId: string;
  sessionTitle: string;
  status: 'running' | 'completed' | 'failed';
  startedAt: number;
  error?: string;
}

interface BackgroundTasksContextValue {
  tasks: BackgroundTask[];
  startAudit: (sessionId: string, sessionTitle: string, model: string, llmApiKey: string, provider?: string) => void;
  dismissTask: (id: string) => void;
}

const BackgroundTasksContext = createContext<BackgroundTasksContextValue | null>(null);

export function useBackgroundTasks() {
  const ctx = useContext(BackgroundTasksContext);
  if (!ctx) throw new Error('useBackgroundTasks must be inside BackgroundTasksProvider');
  return ctx;
}

export function BackgroundTasksProvider({ children }: { children: ReactNode }) {
  const { auth } = useAuth();
  const [tasks, setTasks] = useState<BackgroundTask[]>([]);
  const pollingRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());

  const startAudit = useCallback((sessionId: string, sessionTitle: string, model: string, llmApiKey: string, provider?: string) => {
    if (!auth) return;

    const taskId = `audit-${sessionId}-${Date.now()}`;
    const task: BackgroundTask = {
      id: taskId,
      type: 'audit',
      sessionId,
      sessionTitle: sessionTitle || sessionId.slice(0, 12),
      status: 'running',
      startedAt: Date.now(),
    };

    setTasks(prev => [...prev, task]);

    // Fire the audit request
    auth.client.runAudit(sessionId, model, llmApiKey, provider).then(() => {
      // If it returns synchronously (small session), mark complete immediately
      // Check if the response was 202 by trying to get the report
      return auth.client.getAudit(sessionId);
    }).then(() => {
      setTasks(prev => prev.map(t => t.id === taskId ? { ...t, status: 'completed' } : t));
    }).catch(() => {
      // 202 accepted — start polling
      const interval = setInterval(async () => {
        try {
          await auth.client.getAudit(sessionId);
          // Report exists — done
          setTasks(prev => prev.map(t => t.id === taskId ? { ...t, status: 'completed' } : t));
          clearInterval(interval);
          pollingRef.current.delete(taskId);
        } catch {
          // Still running — keep polling
        }
      }, 5000);
      pollingRef.current.set(taskId, interval);
    });
  }, [auth]);

  const dismissTask = useCallback((id: string) => {
    setTasks(prev => prev.filter(t => t.id !== id));
    const interval = pollingRef.current.get(id);
    if (interval) {
      clearInterval(interval);
      pollingRef.current.delete(id);
    }
  }, []);

  // Auto-dismiss completed tasks after 10 seconds
  useEffect(() => {
    const completedTasks = tasks.filter(t => t.status === 'completed' || t.status === 'failed');
    if (completedTasks.length === 0) return;

    const timeouts = completedTasks.map(t => {
      return setTimeout(() => dismissTask(t.id), 10000);
    });
    return () => timeouts.forEach(clearTimeout);
  }, [tasks, dismissTask]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      pollingRef.current.forEach(interval => clearInterval(interval));
    };
  }, []);

  return (
    <BackgroundTasksContext.Provider value={{ tasks, startAudit, dismissTask }}>
      {children}
      <TaskToasts tasks={tasks} onDismiss={dismissTask} />
    </BackgroundTasksContext.Provider>
  );
}

function TaskToasts({ tasks, onDismiss }: { tasks: BackgroundTask[]; onDismiss: (id: string) => void }) {
  if (tasks.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
      {tasks.map(task => (
        <div
          key={task.id}
          className={`
            flex items-start gap-3 px-4 py-3 rounded-lg border shadow-lg backdrop-blur-sm
            ${task.status === 'running' ? 'bg-bg-secondary/95 border-accent/30' : ''}
            ${task.status === 'completed' ? 'bg-bg-secondary/95 border-green-500/30' : ''}
            ${task.status === 'failed' ? 'bg-bg-secondary/95 border-red-500/30' : ''}
          `}
        >
          {/* Icon */}
          <div className="mt-0.5">
            {task.status === 'running' && (
              <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
            )}
            {task.status === 'completed' && (
              <svg className="w-4 h-4 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
            )}
            {task.status === 'failed' && (
              <svg className="w-4 h-4 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            )}
          </div>

          {/* Content */}
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-text-primary truncate">
              {task.status === 'running' && 'Auditing session...'}
              {task.status === 'completed' && 'Audit complete'}
              {task.status === 'failed' && 'Audit failed'}
            </div>
            <div className="text-sm text-text-secondary truncate">
              {task.sessionTitle}
            </div>
            {task.status === 'running' && (
              <div className="text-sm text-text-muted mt-1">
                {Math.round((Date.now() - task.startedAt) / 1000)}s elapsed
              </div>
            )}
          </div>

          {/* Dismiss */}
          <button
            onClick={() => onDismiss(task.id)}
            className="text-text-muted hover:text-text-secondary transition-colors"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}
