import type { ToolUseInfo } from './types';
import {
  TerminalIcon, FileIcon, EditIcon, SearchIcon, GlobeIcon, TodoIcon, BotIcon, DefaultToolIcon,
} from './icons';

export function describeToolUse(name: string, input: Record<string, unknown> | undefined): ToolUseInfo {
  const inp = input ?? {};

  switch (name) {
    case 'Read': {
      const fp = String(inp.file_path ?? '');
      return { icon: <FileIcon />, summary: fp || 'Read file' };
    }
    case 'Write': {
      const fp = String(inp.file_path ?? '');
      return { icon: <FileIcon />, summary: fp || 'Write file' };
    }
    case 'Edit': {
      const fp = String(inp.file_path ?? '');
      return { icon: <EditIcon />, summary: fp ? `${fp} — edit` : 'Edit' };
    }
    case 'MultiEdit': {
      const fp = String(inp.file_path ?? '');
      return { icon: <EditIcon />, summary: fp ? `${fp} — edit` : 'MultiEdit' };
    }
    case 'Bash': {
      const cmd = String(inp.command ?? '');
      return { icon: <TerminalIcon />, summary: cmd || 'Bash' };
    }
    case 'Grep': {
      const pattern = String(inp.pattern ?? '');
      const path = inp.path ? String(inp.path) : '';
      return { icon: <SearchIcon />, summary: path ? `${pattern} in ${path}` : pattern || 'Grep' };
    }
    case 'Glob': {
      const pattern = String(inp.pattern ?? '');
      const path = inp.path ? String(inp.path) : '';
      return { icon: <SearchIcon />, summary: path ? `${pattern} in ${path}` : pattern || 'Glob' };
    }
    case 'WebFetch': {
      const url = String(inp.url ?? '');
      return { icon: <GlobeIcon />, summary: url || 'WebFetch' };
    }
    case 'WebSearch': {
      const query = String(inp.query ?? '');
      return { icon: <GlobeIcon />, summary: query || 'WebSearch' };
    }
    case 'TodoWrite': {
      const todos = inp.todos as Array<Record<string, unknown>> | undefined;
      const count = todos?.length ?? 0;
      return { icon: <TodoIcon />, summary: count === 1 ? '1 todo' : `${count} todos` };
    }
    case 'Task': {
      const desc = String(inp.description ?? inp.subagent_type ?? '');
      return { icon: <BotIcon />, summary: desc || 'Task' };
    }
    case 'TaskOutput': {
      const taskId = String(inp.task_id ?? '');
      return { icon: <BotIcon />, summary: taskId || 'Task output' };
    }
    case 'Agent': {
      const desc = String(inp.description ?? inp.prompt ?? '');
      return { icon: <BotIcon />, summary: desc || 'Agent' };
    }
    default: {
      const keys = Object.keys(inp).slice(0, 3);
      const summary = keys.length > 0 ? keys.join(', ') : name;
      return { icon: <DefaultToolIcon />, summary };
    }
  }
}
