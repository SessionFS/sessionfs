import { useRef, useState, type DragEvent, type FormEvent } from 'react';
import type { MeetingDetail, MeetingScope } from '../api/client';
import { Alert, Button, Checkbox, Dialog, DialogFooter, DialogHeader, Input, Textarea } from '../components/ui';
import { useUploadMeeting } from '../hooks/useMeetings';
import {
  ACCEPTED_EXTENSIONS,
  MAX_TITLE,
  byteLength,
  contentTooLarge,
  fileTooLarge,
  hasAcceptedExtension,
  isCaptionFile,
  lineCount,
  meetingAtFromDate,
  readFileText,
  sizeLabel,
  titleFromFileName,
  uploadErrorMessage,
} from './format';

interface UploadMeetingDialogProps {
  scope: MeetingScope;
  /** Where it goes, for the dialog's subtitle ("Acme widgets"). */
  scopeLabel?: string;
  onClose: () => void;
  /** Called with the stored meeting (and its warnings) after a save. */
  onUploaded: (meeting: MeetingDetail) => void;
}

interface PickedFile {
  name: string;
  size: number;
  text: string;
}

/**
 * Add a meeting: paste the transcript or pick a file (read here, in the
 * browser — nothing is uploaded until Save). The scope is fixed by the page.
 */
export function UploadMeetingDialog({ scope, scopeLabel, onClose, onUploaded }: UploadMeetingDialogProps) {
  const upload = useUploadMeeting(scope);
  const fileInput = useRef<HTMLInputElement>(null);

  const [title, setTitle] = useState('');
  const [titleTouched, setTitleTouched] = useState(false);
  const [text, setText] = useState('');
  const [file, setFile] = useState<PickedFile | null>(null);
  const [date, setDate] = useState('');
  const [extract, setExtract] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const content = file ? file.text : text;

  async function takeFile(f: File | undefined) {
    if (!f) return;
    setError(null);
    if (!hasAcceptedExtension(f.name)) {
      setFileError(`Choose a ${ACCEPTED_EXTENSIONS.join(', ')} file.`);
      return;
    }
    // Never read a file the server would refuse outright.
    const tooBig = fileTooLarge(f.size);
    if (tooBig) {
      setFileError(tooBig);
      return;
    }
    try {
      const body = await readFileText(f);
      setFile({ name: f.name, size: f.size, text: body });
      setFileError(contentTooLarge(body, isCaptionFile(f.name)));
      if (!titleTouched || !title.trim()) setTitle(titleFromFileName(f.name));
    } catch {
      setFileError('That file could not be read.');
    }
  }

  function clearFile() {
    setFile(null);
    setFileError(null);
    if (fileInput.current) fileInput.current.value = '';
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    void takeFile(e.dataTransfer.files?.[0]);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!title.trim()) {
      setError('Give the meeting a title.');
      return;
    }
    if (!content.trim()) {
      setError('Paste the transcript or choose a file.');
      return;
    }
    const tooLarge = contentTooLarge(content, !!file && isCaptionFile(file.name));
    if (tooLarge) {
      setError(tooLarge);
      return;
    }
    setError(null);
    upload.mutate(
      {
        title: title.trim().slice(0, MAX_TITLE),
        content,
        source: file ? 'file_upload' : 'manual_paste',
        meeting_at: meetingAtFromDate(date),
        extract,
      },
      {
        onSuccess: (meeting) => onUploaded(meeting),
        onError: (err) => setError(uploadErrorMessage(err, !!file && isCaptionFile(file.name))),
      },
    );
  }

  const lines = lineCount(content);

  return (
    <Dialog
      open
      // While saving (and extracting, up to two minutes) the dialog stays:
      // closing it would look like a cancel of an upload that still lands.
      onClose={() => {
        if (!upload.isPending) onClose();
      }}
      titleId="meeting-upload-title"
      className="max-w-2xl"
    >
      <DialogHeader titleId="meeting-upload-title">Add a meeting</DialogHeader>
      <p className="-mt-2 mb-4 text-sm text-text-tertiary">
        {scopeLabel ? <>Saved to {scopeLabel}. </> : null}
        Transcripts are checked against your data protection policy before they’re stored.
      </p>

      <form onSubmit={submit} className="space-y-4" noValidate>
        <Input
          id="meeting-title"
          title="Title"
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setTitleTouched(true);
          }}
          maxLength={MAX_TITLE}
          placeholder="Weekly platform sync"
          required
        />

        <div>
          <div className="mb-1 flex items-baseline justify-between gap-2">
            <label htmlFor={file ? undefined : 'meeting-content'} className="text-sm font-medium text-text-secondary">
              Transcript
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
                ({ACCEPTED_EXTENSIONS.join(' ')})
              </span>
            )}
          </div>
          <input
            ref={fileInput}
            type="file"
            accept={ACCEPTED_EXTENSIONS.join(',')}
            className="sr-only"
            tabIndex={-1}
            aria-label="Transcript file"
            onChange={(e) => void takeFile(e.target.files?.[0])}
          />

          {file ? (
            <div className="flex items-center gap-3 rounded-lg border border-border bg-bg-primary px-3 py-2.5">
              <FileIcon />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-text-primary">{file.name}</p>
                <p className="text-xs text-text-tertiary">
                  {sizeLabel(file.size)} · {lines.toLocaleString()} {lines === 1 ? 'line' : 'lines'}
                </p>
              </div>
              <Button variant="ghost" size="sm" type="button" onClick={clearFile}>
                Remove
              </Button>
            </div>
          ) : (
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              className={`rounded-lg transition-shadow ${dragging ? 'shadow-[0_0_0_2px_var(--brand)]' : ''}`}
            >
              <Textarea
                id="meeting-content"
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={10}
                className="font-mono text-xs leading-5"
                placeholder={'Priya: Let’s lock the launch date today.\nSam: I’ll draft the rollout plan by Friday.'}
                hint={
                  text
                    ? `${lines.toLocaleString()} ${lines === 1 ? 'line' : 'lines'} · ${sizeLabel(byteLength(text))} of 512 KB`
                    : 'Paste the text, or drop a file here. Speaker names at the start of lines help match people.'
                }
              />
            </div>
          )}
          {fileError && <p className="mt-1 text-xs text-danger" role="alert">{fileError}</p>}
        </div>

        <div className="grid gap-3 sm:grid-cols-2 items-start">
          <Input
            id="meeting-date"
            type="date"
            title="Meeting date (optional)"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
        </div>

        <Checkbox
          id="meeting-extract"
          checked={extract}
          onChange={(e) => setExtract(e.target.checked)}
          label="Extract action items now"
          description="Uses your AI key, or your organization’s. You can also do this later."
        />

        {error && <Alert variant="danger">{error}</Alert>}

        <DialogFooter>
          <Button variant="secondary" type="button" onClick={onClose} disabled={upload.isPending}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={upload.isPending} disabled={upload.isPending || !!fileError}>
            {upload.isPending && extract ? 'Saving and extracting…' : 'Add meeting'}
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

function FileIcon() {
  return (
    <span aria-hidden="true" className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-bg-elevated text-text-tertiary">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="8" y1="13" x2="16" y2="13" />
        <line x1="8" y1="17" x2="13" y2="17" />
      </svg>
    </span>
  );
}
