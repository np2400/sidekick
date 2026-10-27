import { useCallback, useEffect, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent } from "react";
import { useParams } from "react-router-dom";
import { getConversation, sendMessage, type Unavailable } from "../api";
import { EmptyState, Skeleton, TelegramHint } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import { useResult } from "../useResult";

/** Ideas only fill the composer: they show what to ask, they are not conversations. */
const IDEAS: { icon: IconName; text: string }[] = [
  { icon: "calendar", text: "What's on my calendar tomorrow?" },
  { icon: "bell", text: "Remind me to call Mom at 6pm" },
  { icon: "mail", text: "Summarize my unread email" },
  { icon: "file", text: "Find my hotel bookings for December" },
];

const ACCEPT = "image/*,application/pdf";
const MAX_MB = 10;

type Attachment = { id: string; file: File; preview?: string };

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric",
                                            minute: "2-digit" });

/** One saved conversation, read-only: exactly what was said, in Telegram or here. */
function Thread({ id }: { id: string }) {
  const load = useCallback(() => getConversation(id), [id]);
  const result = useResult(load);
  if (result === null) return <div className="thread"><Skeleton lines={6} /></div>;
  if (result.state !== "ready") {
    return <EmptyState icon="history" title="Couldn't open this conversation"
      body={result.state === "unavailable" ? result.reason : "Sign in to see your conversations."} />;
  }
  const thread = result.data;
  return (
    <div className="thread">
      <header className="thread-head">
        <h1>{thread.title}</h1>
        <p className="muted small">
          {thread.origin === "telegram" ? "Started in Telegram" : "Started here"} · kept for 30 days after the last message
        </p>
      </header>
      {thread.messages.map((m, i) => (
        <div key={i} className={`msg ${m.role}`}>
          <div className="msg-bubble">{m.text}</div>
          <span className="msg-meta">{m.channel === "telegram" ? "Telegram · " : ""}{when(m.at)}</span>
        </div>
      ))}
    </div>
  );
}

function formatSize(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function Chat() {
  const { id } = useParams();
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [notice, setNotice] = useState<Unavailable | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);

  useEffect(() => {  // grow the box with the text, up to a limit
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const latest = useRef(files);  // previews are browser memory: free them when leaving the page
  latest.current = files;
  useEffect(() => () => latest.current.forEach((f) => f.preview && URL.revokeObjectURL(f.preview)), []);

  function addFiles(list: FileList | File[]) {
    const accepted: Attachment[] = [];
    const rejected: string[] = [];
    for (const file of Array.from(list)) {
      const ok = file.type.startsWith("image/") || file.type === "application/pdf";
      if (!ok) rejected.push(`${file.name} (only photos and PDFs)`);
      else if (file.size > MAX_MB * 1024 * 1024) rejected.push(`${file.name} (over ${MAX_MB} MB)`);
      else accepted.push({ id: `${file.name}-${file.size}-${file.lastModified}-${Math.random()}`, file,
        preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined });
    }
    setFiles((current) => [...current, ...accepted]);
    setProblem(rejected.length ? `Not added: ${rejected.join(", ")}.` : null);
  }

  function removeFile(id: string) {
    setFiles((current) => {
      const gone = current.find((f) => f.id === id);
      if (gone?.preview) URL.revokeObjectURL(gone.preview);
      return current.filter((f) => f.id !== id);
    });
  }

  async function submit(e?: FormEvent) {
    e?.preventDefault();
    if (!text.trim() && files.length === 0) return;
    const result = await sendMessage(text, files.map((f) => f.file));
    if (result.state === "unavailable") setNotice(result);  // nothing was sent: keep what they wrote
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  }

  const canSend = text.trim().length > 0 || files.length > 0;

  return (
    <div className={`chat${dragging ? " dragging" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false); }}
      onDrop={onDrop}>
      <div className="chat-scroll">
        {id ? <Thread id={id} /> : <div className="chat-welcome">
          <span className="welcome-mark"><Icon name="sparkles" size={26} /></span>
          <h1>What can I help with?</h1>
          <p className="muted">
            Ask about your calendar, email, reminders and tasks, or send a photo or PDF. Your conversations,
            including the ones in Telegram, are listed on the left; sending from the web comes in the next step.
          </p>
          <div className="ideas">
            {IDEAS.map((idea) => (
              <button key={idea.text} type="button" className="idea"
                onClick={() => { setText(idea.text); input.current?.focus(); }}>
                <Icon name={idea.icon} size={18} />
                <span>{idea.text}</span>
              </button>
            ))}
          </div>
        </div>}
      </div>

      <div className="composer-wrap">
        {notice && (
          <div className="notice" role="status">
            <Icon name="info" size={18} />
            <div>
              <p>{notice.reason}</p>
              {notice.today && <p className="muted small">{notice.today}</p>}
            </div>
            <button className="icon-btn small" onClick={() => setNotice(null)} aria-label="Dismiss"><Icon name="x" size={16} /></button>
          </div>
        )}

        <form className="composer" onSubmit={submit}>
          {files.length > 0 && (
            <ul className="attachments" aria-label="Attached files">
              {files.map((a) => (
                <li key={a.id} className="attachment">
                  {a.preview ? <img src={a.preview} alt="" /> : <span className="file-badge"><Icon name="file" size={18} /></span>}
                  <span className="attachment-text">
                    <span className="attachment-name">{a.file.name}</span>
                    <span className="attachment-meta">{a.file.type === "application/pdf" ? "PDF" : "Photo"} · {formatSize(a.file.size)}</span>
                  </span>
                  <button type="button" className="attachment-remove" onClick={() => removeFile(a.id)}
                    aria-label={`Remove ${a.file.name}`}><Icon name="x" size={14} /></button>
                </li>
              ))}
            </ul>
          )}
          <label htmlFor="message" className="sr-only">Message Sidekick</label>
          <textarea id="message" ref={input} rows={1} value={text} placeholder="Message Sidekick"
            onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown} />
          <div className="composer-bar">
            <button type="button" className="icon-btn" onClick={() => picker.current?.click()}
              aria-label="Attach photos or PDFs" title="Attach photos or PDFs">
              <Icon name="paperclip" />
            </button>
            <input ref={picker} type="file" accept={ACCEPT} multiple hidden
              onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
            <span className="composer-spacer" />
            <button type="submit" className="send-btn" disabled={!canSend} aria-label="Send">
              <Icon name="arrowUp" size={18} />
            </button>
          </div>
        </form>
        {problem && <p className="composer-problem" role="alert">{problem}</p>}
        <p className="composer-foot">
          Read-only preview: sending from the web comes next. <TelegramHint>message @Np_Sidekick_bot</TelegramHint>
        </p>
      </div>

      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          <Icon name="paperclip" size={28} />
          <p>Drop photos or PDFs</p>
        </div>
      )}
    </div>
  );
}
