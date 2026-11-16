import { Bot, ChevronRight, Copy, File, Globe, ListTodo, Pencil, Search, Terminal, Wrench } from 'lucide-react';
import { Icon } from '../../components/ui/Icon';

/* Tool-block icons for the session transcript, all decorative. */

export function TerminalIcon() {
  return <Icon icon={Terminal} />;
}

export function FileIcon() {
  return <Icon icon={File} />;
}

export function EditIcon() {
  return <Icon icon={Pencil} />;
}

export function SearchIcon() {
  return <Icon icon={Search} />;
}

export function GlobeIcon() {
  return <Icon icon={Globe} />;
}

export function TodoIcon() {
  return <Icon icon={ListTodo} />;
}

export function BotIcon() {
  return <Icon icon={Bot} />;
}

export function DefaultToolIcon() {
  return <Icon icon={Wrench} />;
}

export function ChevronIcon({ open }: { open: boolean }) {
  return (
    <Icon
      icon={ChevronRight}
      size={14}
      className={`transition-transform duration-150 ${open ? 'rotate-90' : 'rotate-0'}`}
    />
  );
}

export function CopyIcon() {
  return <Icon icon={Copy} size={14} />;
}
