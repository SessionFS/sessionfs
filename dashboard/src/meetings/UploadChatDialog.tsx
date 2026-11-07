import { useRef, useState, type FormEvent } from 'react';
import type { ChatProvider, MeetingDetail, MeetingScope } from '../api/client';
import { Alert, Button, Checkbox, Dialog, DialogFooter, DialogHeader, Input, Select, Textarea } from '../components/ui';
import { useUploadChatThread } from '../hooks/useMeetings';
import {
  CHAT_ACCEPTED_EXTENSIONS,
  CHAT_PROVIDERS,
  CHAT_TOO_LARGE,
  MAX_CHANNEL,
  MAX_CHAT_BYTES,
  MAX_CHAT_TITLE,
  chatTooLarge,
  chatUploadErrorMessage,
  hasChatExtension,
  isJsonProvider,
  needsAttestation,
} from './chat';
import { byteLength, readFileText, sizeLabel, titleFromFileName } from './format';

interface UploadChatDialogProps {
  scope: MeetingScope;
  scopeLabel?: string;
  onClose: () => void;
  onUploaded: (thread: MeetingDetail) => void;
}

interface Picked {
  name: string;
  size: number;
  text: string;
}

/**
 * Add a chat thread: pick the provider, name the channel, then choose a file
 * or paste the text (read here, in the browser; nothing is sent until Save).
 */
export function UploadChatDialog({ scope, scopeLabel, onClose, onUploaded }: UploadChatDialogProps) {
  const upload = useUploadChatThread(scope);
  const fileInput = useRef<HTMLInputElement>(null);
  const usersInput = useRef<HTMLInputElement>(null);

  const [provider, setProvider] = useState<ChatProvider>('slack');
  const [channel, setChannel] = useState('');
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [file, setFile] = useState<Picked | null>(null);
  const [users, setUsers] = useState<Picked | null>(null);
  const [attested, setAttested] = useState(false);
  const [extract, setExtract] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [usersError, setUsersError] = useState<string | null>(null);

  const json = isJsonProvider(provider);
  const content = file ? file.text : text;
  const busy = upload.isPending;
  const attestationNeeded = needsAttestation(provider, content);

  async function readPicked(f: File | undefined): Promise<Picked | string | null> {
    if (!f) return null;
    if (!hasChatExtension(f.name)) return `Choose a ${CHAT_ACCEPTED_EXTENSIONS.join(', ')} file.`;
    // Never read a file the server would refuse outright.
    if (f.size > MAX_CHAT_BYTES) return CHAT_TOO_LARGE;
    try {
      return { name: f.name, size: f.size, text: await readFileText(f) };
    } catch {
      return 'That file could not be read.';
    }
  }

  async function takeFile(f: File | undefined) {
    setError(null);
    const r = await readPicked(f);
    if (r === null) return;
    if (typeof r === 'string') {
      setFileError(r);
      return;
    }
    setFile(r);
    setFileError(chatTooLarge(r.text));
    if (!title.trim()) setTitle(titleFromFileName(r.name).slice(0, MAX_CHAT_TITLE));
  }

  async function takeUsers(f: File | undefined) {
    const r = await readPicked(f);
    if (r === null) return;
    if (typeof r === 'string') {
      setUsersError(r);
      return;
    }
    setUsers(r);
    setUsersError(chatTooLarge(r.text));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const name = channel.trim();
    if (!name) {
      setError('Name the channel, for example #infra.');
      return;
    }
    if (!content.trim()) {
      setError(json ? 'Choose the export file or paste its JSON.' : 'Paste the thread or choose a file.');
      return;
    }
    const tooLarge = chatTooLarge(content) ?? (users ? chatTooLarge(users.text) : null);
    if (tooLarge) {
      setError(tooLarge);
      return;
    }
    if (attestationNeeded && !attested) {
      setError('Confirm that this thread comes from a channel, not a direct message.');
      return;
    }
    setError(null);
    upload.mutate(
      {
        provider,
        channel_name: name.slice(0, MAX_CHANNEL),
        content,
        ...(attestationNeeded ? { attestation: true } : {}),
        ...(provider === 'slack' && users ? { users: users.text } : {}),
        ...(title.trim() ? { title: title.trim().slice(0, MAX_CHAT_TITLE) } : {}),
        extract,
      },
      {
        onSuccess: (thread) => onUploaded(thread),
        onError: (err) => setError(chatUploadErrorMessage(err)),
      },
    );
  }

  return (
    <Dialog
      open
      // While saving (and extracting) the dialog stays: closing it would look
      // like a cancel of an upload that still lands.
      onClose={() => {
        if (!busy) onClose();
      }}
      titleId="chat-upload-title"
      className="max-w-2xl"
    >
      <DialogHeader titleId="chat-upload-title">Add a chat thread</DialogHeader>
      <p className="-mt-2 mb-4 text-sm text-text-tertiary">
        {scopeLabel ? <>Saved to {scopeLabel}. </> : null}
        Threads are checked against your data protection policy before they’re stored. Direct messages can’t be added.
      </p>

      <form onSubmit={submit} className="space-y-4" noValidate>
        <fieldset disabled={busy} className="min-w-0 space-y-4 border-0 p-0">
          <div className="grid items-start gap-3 sm:grid-cols-2">
            <Select
              id="chat-provider"
              title="Source"
              value={provider}
              onValueChange={(v) => {
                setProvider(v as ChatProvider);
                setError(null);
              }}
              options={CHAT_PROVIDERS}
            />
            <Input
              id="chat-channel"
              title="Channel name"
              value={channel}
              onChange={(e) => setChannel(e.target.value)}
              maxLength={MAX_CHANNEL}
              placeholder="#infra"
              hint="A label only. 1 to 80 characters."
              required
            />
          </div>

          <Input
            id="chat-title"
            title="Title (optional)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={MAX_CHAT_TITLE}
            placeholder="LB certificate decision"
          />

          <div>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <label htmlFor={file ? undefined : 'chat-content'} className="text-sm font-medium text-text-secondary">
                {json ? 'Export' : 'Thread'}
              </label>
              {!file && (
                <span className="text-xs text-text-tertiary">
                  or{' '}
                  <button
                    type="button"
                    onClick={() => fileInput.current?.click()}
                    className="rounded-sm text-brand underline-offset-2 hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  >
                    choose a file
                  </button>{' '}
                  ({CHAT_ACCEPTED_EXTENSIONS.join(' ')})
                </span>
              )}
            </div>
            <input
              ref={fileInput}
              type="file"
              accept={CHAT_ACCEPTED_EXTENSIONS.join(',')}
              className="sr-only"
              tabIndex={-1}
              aria-label="Chat export file"
              onChange={(e) => void takeFile(e.target.files?.[0])}
            />
            {file ? (
              <div className="flex items-center gap-3 rounded-lg border border-border bg-bg-primary px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-text-primary">{file.name}</p>
                  <p className="text-xs text-text-tertiary">{sizeLabel(file.size)}</p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  type="button"
                  onClick={() => {
                    setFile(null);
                    setFileError(null);
                    if (fileInput.current) fileInput.current.value = '';
                  }}
                >
                  Remove
                </Button>
              </div>
            ) : (
              <Textarea
                id="chat-content"
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={9}
                className="font-mono text-xs leading-5"
                placeholder={json ? '[ { "user": "U123", "text": "…", "ts": "…" } ]' : 'Tolu: We are going with Dataproc.\nSam: I’ll own the ticket.'}
                hint={
                  text
                    ? `${sizeLabel(byteLength(text))} of 2 MB`
                    : json
                      ? 'Paste the JSON, or choose the file from your export.'
                      : 'Paste the text. “Name: text” lines, or blocks separated by a blank line.'
                }
              />
            )}
            {fileError && (
              <p className="mt-1 text-xs text-danger" role="alert">
                {fileError}
              </p>
            )}
          </div>

          {provider === 'slack' && (
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-text-secondary">users.json (optional)</span>
                {users ? (
                  <>
                    <span className="min-w-0 truncate text-xs text-text-tertiary">{users.name}</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      type="button"
                      onClick={() => {
                        setUsers(null);
                        setUsersError(null);
                        if (usersInput.current) usersInput.current.value = '';
                      }}
                    >
                      Remove
                    </Button>
                  </>
                ) : (
                  <Button variant="secondary" size="sm" type="button" onClick={() => usersInput.current?.click()}>
                    Choose users.json
                  </Button>
                )}
              </div>
              <input
                ref={usersInput}
                type="file"
                accept=".json"
                className="sr-only"
                tabIndex={-1}
                aria-label="Slack users.json file"
                onChange={(e) => void takeUsers(e.target.files?.[0])}
              />
              <p className="mt-1 text-xs text-text-tertiary">Shows names instead of user ids.</p>
              {usersError && (
                <p className="mt-1 text-xs text-danger" role="alert">
                  {usersError}
                </p>
              )}
            </div>
          )}

          {attestationNeeded && (
            <Checkbox
              id="chat-attest"
              checked={attested}
              onChange={(e) => setAttested(e.target.checked)}
              label="This is from a channel, not a direct message"
              description={
                isJsonProvider(provider)
                  ? 'This export has no channel details, so it can’t be checked. Please confirm.'
                  : 'Pasted text can’t be checked, so please confirm.'
              }
            />
          )}

          <Checkbox
            id="chat-extract"
            checked={extract}
            onChange={(e) => setExtract(e.target.checked)}
            label="Extract action items now"
            description="Uses your AI key, or your organization’s. You can also do this later."
          />
        </fieldset>

        {error && <Alert variant="danger">{error}</Alert>}

        <DialogFooter>
          <Button variant="secondary" type="button" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={busy} disabled={busy || !!fileError || !!usersError}>
            {busy && extract ? 'Saving and extracting…' : 'Add chat thread'}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
