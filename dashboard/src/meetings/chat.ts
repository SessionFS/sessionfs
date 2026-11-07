/**
 * Pure helpers for chat threads (GitHub #118): providers, the 2 MiB client
 * limit, error codes in words, and turning a stored thread into numbered
 * messages. Free of React so they are cheap to unit-test.
 */

import { ApiError, type ChatProvider, type MeetingDetail, type MeetingSummary } from '../api/client';
import { errorMessage } from '../utils/errorMessage';
import { byteLength, describeFindings, dlpFindings, meetingErrorCode } from './format';

/** The raw export limit (routes/meetings.py): 2 MiB of Slack / Discord JSON or pasted text. */
export const MAX_CHAT_BYTES = 2 * 1024 * 1024;
export const MAX_CHANNEL = 80;
export const MAX_CHAT_TITLE = 200;

/** The provider values `ChatThreadCreate` accepts, in the order the picker lists them. */
export const CHAT_PROVIDERS: { value: ChatProvider; label: string }[] = [
  { value: 'slack', label: 'Slack export (JSON)' },
  { value: 'discord', label: 'Discord export (JSON)' },
  { value: 'paste', label: 'Pasted text' },
  { value: 'teams_paste', label: 'Pasted from Teams' },
];

export const PROVIDER_LABEL: Record<string, string> = {
  slack: 'Slack',
  discord: 'Discord',
  paste: 'Paste',
  teams_paste: 'Teams (paste)',
};

export const providerLabel = (p: string | null | undefined): string => (p ? PROVIDER_LABEL[p] ?? p : 'Chat');

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

/**
 * A Slack export with no `channel` / `channel_info` object (a bare message
 * array) can't be told apart from a DM, so the server needs an attestation
 * for it. Unreadable JSON is left to the server to report.
 */
export function slackLacksChannelObject(content: string): boolean {
  let data: unknown;
  try {
    data = JSON.parse(content);
  } catch {
    return false;
  }
  if (Array.isArray(data)) return true;
  if (isObject(data)) return !isObject(data.channel) && !isObject(data.channel_info);
  return false;
}

/** Guild channel kinds a Discord export may carry without an attestation (the server's allowlist). */
const DISCORD_GUILD_TYPES = new Set([
  'guildtextchat', 'guildvoicechat', 'guildcategory', 'guildnews',
  'guildnewsthread', 'guildpublicthread', 'guildprivatethread',
  'guildstagevoice', 'guilddirectory', 'guildforum', 'guildmedia',
  '0', '2', '4', '5', '10', '11', '12', '13', '14', '15', '16',
]);

/**
 * A Discord export that doesn't prove it is a guild channel (guild and
 * channel objects with a known guild channel type) needs an attestation.
 * Unreadable JSON, or JSON that isn't an export, is left to the server.
 */
export function discordLacksGuildChannel(content: string): boolean {
  let data: unknown;
  try {
    data = JSON.parse(content);
  } catch {
    return false;
  }
  if (!isObject(data) || !Array.isArray(data.messages)) return false;
  if (!isObject(data.guild) || !isObject(data.channel)) return true;
  const type = data.channel.type;
  if (typeof type !== 'string' && typeof type !== 'number') return true;
  return !DISCORD_GUILD_TYPES.has(String(type).trim().toLowerCase());
}

/**
 * A paste can't be checked for being a DM, so it needs an attestation; so
 * does a Slack export with no channel object and a Discord export without
 * guild channel details (same rules as the server).
 */
export function needsAttestation(p: ChatProvider, content = ''): boolean {
  if (p === 'paste' || p === 'teams_paste') return true;
  if (!content.trim()) return false;
  if (p === 'slack') return slackLacksChannelObject(content);
  return discordLacksGuildChannel(content);
}
export const isJsonProvider = (p: ChatProvider): boolean => p === 'slack' || p === 'discord';

export const isChat = (m: Pick<MeetingSummary, 'kind'> | undefined | null): boolean => m?.kind === 'chat';

export const CHAT_ACCEPTED_EXTENSIONS = ['.json', '.txt', '.md'];
export const hasChatExtension = (name: string): boolean =>
  CHAT_ACCEPTED_EXTENSIONS.some((ext) => name.toLowerCase().endsWith(ext));

export const CHAT_TOO_LARGE = 'This is larger than 2 MB. Export a shorter range or a single thread and add each part.';

/** Null when the content fits the raw limit. */
export function chatTooLarge(text: string): string | null {
  return byteLength(text) > MAX_CHAT_BYTES ? CHAT_TOO_LARGE : null;
}

/** Plain-language message for a failed chat upload. */
export function chatUploadErrorMessage(err: unknown): string {
  const code = meetingErrorCode(err);
  switch (code) {
    case 'dm_not_supported':
      return 'Direct-message and group-DM exports can’t be uploaded. Upload a channel instead.';
    case 'attestation_required':
      return 'Confirm that this thread comes from a channel, not a direct message.';
    case 'chat_unparseable':
      return err instanceof ApiError && err.message
        ? err.message
        : 'That file couldn’t be read as a chat export for this provider.';
    case 'chat_too_large':
    case 'meeting_too_large':
      return 'This thread is too large. Chat threads hold up to 2 MB of export and 512 KB and 10,000 messages once stored. Add a shorter range.';
    case 'dlp_blocked':
      return `Your organization’s data protection policy blocked this thread: it contains ${describeFindings(dlpFindings(err))}. Remove those and try again.`;
    case 'group_edit_forbidden':
      return 'You can view chat threads here but not add them.';
    case 'validation_error':
      return 'Check the channel name and provider, then try again.';
    default:
      if (err instanceof ApiError && err.status === 413) {
        return 'This thread is too large. Add a shorter range.';
      }
      if (err instanceof ApiError && err.status === 403) return 'You can view chat threads here but not add them.';
      return errorMessage(err, 'The chat thread could not be saved.');
  }
}

// ── Messages ──

export interface ChatMessage {
  /** 1-based message number (= line number in the stored thread). */
  n: number;
  author: string | null;
  postedAt: string | null;
  text: string;
}

const PREFIX_RE = /^\[[^\]]*\]\s*/;

/** `[2026-10-29 14:02] Tolu: text` → author/time from the side table, text without the prefix. */
export function chatMessages(thread: Pick<MeetingDetail, 'content' | 'messages'>): ChatMessage[] {
  if (!thread.content) return [];
  const lines = thread.content.split('\n');
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  const meta = new Map((thread.messages ?? []).map((m) => [m.line_no, m]));
  return lines.map((raw, i) => {
    const n = i + 1;
    const m = meta.get(n);
    let text = raw;
    if (m) {
      text = text.replace(PREFIX_RE, '');
      if (text.startsWith(`${m.author_raw}:`)) text = text.slice(m.author_raw.length + 1).replace(/^\s/, '');
    }
    // The normaliser folds a message's own newlines into " ⏎ ".
    text = text.split(' ⏎ ').join('\n');
    return { n, author: m?.author_raw ?? null, postedAt: m?.posted_at ?? null, text };
  });
}

const TIME_FMT = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
});

/** "Oct 29, 14:02 UTC", or '' when the time is missing or unreadable. */
export function messageTimeLabel(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : `${TIME_FMT.format(d)} UTC`;
}

/** "Message 4" / "Messages 4–6" for a range. */
export function messagesLabel(range: { start: number; end: number }): string {
  return range.start === range.end ? `Message ${range.start}` : `Messages ${range.start}–${range.end}`;
}
