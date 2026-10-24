import type { ReactNode } from 'react';

export interface BlockProps {
  block: Record<string, unknown>;
}

export interface ToolUseInfo {
  icon: ReactNode;
  summary: string;
}

export interface DiffLine {
  type: 'same' | 'removed' | 'added';
  text: string;
}
