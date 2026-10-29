import {
  useCallback, useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type FormEvent, type KeyboardEvent,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  conversationsChanged, decideProposal, deleteConversation, getConversation, jobStatus, sendMessage, type Attached,
  type Message,
  type Result, type Transcript, type Unavailable,
} from "../api";
import { EmptyState, Skeleton } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";

/** Ideas only fill the composer: they show what to ask, they are not conversations. */
const IDEAS: { icon: IconName; text: string }[] = [
  { icon: "calendar", text: "What's on my calendar tomorrow?" },
  { icon: "bell", text: "Remind me to call Mom at 6pm" },
  { icon: "mail", text: "Summarize my unread email" },
  { icon: "file", text: "Find my hotel bookings for December" },
];

const ACCEPT = "image/*,application/pdf";
const MAX_PHOTO_MB = 25;  // before it's made smaller here
const MAX_PDF_MB = 5;     // the server's limit too (app/web_api.py)
const MAX_SIDE = 1600;    // a photo's longest side once made smaller: plenty for reading text in it
const SLOW_MS = 20_000;  // after this, say that long answers (web search, email) take a while
const GIVE_UP_MS = 90_000;  // can't reach Sidekick for this long: stop waiting and show what's wrong

/** A photo as a smaller JPEG, in base64. Drawing it anew also leaves out hidden details (like where it was taken). */
async function shrink(file: File): Promise<string> {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });  // upright, as it looks
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const paint = canvas.getContext("2d")!;
  paint.fillStyle = "#fff";  // a see-through PNG gets white behind it, not black
  paint.fillRect(0, 0, canvas.width, canvas.height);
  paint.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const blob = await new Promise<Blob | null>((done) => canvas.toBlob(done, "image/jpeg", 0.85));
  if (!blob) throw new Error("not encoded");
  return base64(blob);
}

function base64(blob: Blob): Promise<string> {
  return new Promise((done, fail) => {
    const reader = new FileReader();
    reader.onload = () => done(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => fail(reader.error);
    reader.readAsDataURL(blob);
  });
}

/** What goes with the message: one photo (made smaller) or one PDF. */
async function prepare(file: File): Promise<Attached & { data: string }> {
  if (file.type === "application/pdf") return { type: "application/pdf", name: file.name, data: await base64(file) };
  return { type: "image/jpeg", name: file.name, data: await shrink(file) };
}

function AttachedLabel({ attachment }: { attachment: Attached }) {
  return <span className="msg-attached">{attachment.type === "application/pdf" ? `📄 ${attachment.name}` : "📷 Photo"}</span>;
}

/** A photo or PDF sent from this page. Only this browser has it, in memory: the chat can show it until the page
 *  is reloaded. Sidekick itself doesn't store it (it keeps its own copy 10 minutes for follow-up questions). */
type LocalFile = { url: string; pdf: boolean; name: string; text: string; jobId: number };
const SENT = new Map<number, LocalFile[]>();  // conversation id -> files sent from this page

/** How the server shows a message that came with this file (app/memory.py MARKERS). */
const shownAs = (f: LocalFile) =>
  f.pdf ? `📄 PDF ${f.name}${f.text ? ` · ${f.text}` : ""}` : `📷 Photo ${f.text}`.trim();

/** Which saved messages came with a file sent from this page: newest first, so an older look-alike isn't matched. */
function withFiles(messages: Message[], files: LocalFile[]): Map<number, LocalFile> {
  const found = new Map<number, LocalFile>();
  const left = [...files];
  for (let i = messages.length - 1; i >= 0 && left.length > 0; i--) {
    if (messages[i].role !== "user") continue;
    for (let k = left.length - 1; k >= 0; k--) {
      if (shownAs(left[k]) === messages[i].text) {
        found.set(i, left[k]);
        left.splice(k, 1);
        break;
      }
    }
  }
  return found;
}

function FileView({ file, onView }: { file: LocalFile; onView: (url: string) => void }) {
  if (file.pdf) {
    return <a className="msg-attached msg-link" href={file.url} target="_blank" rel="noopener">📄 {file.name}</a>;
  }
  return (
    <button type="button" className="msg-photo" onClick={() => onView(file.url)} aria-label="View the photo"
      title="Shown from this browser only: Sidekick doesn't store photos">
      <img src={file.url} alt="Your photo" />
    </button>
  );
}

type Attachment = { id: string; file: File; preview?: string };

const clock = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric",
                                            minute: "2-digit" });

/** One conversation, exactly as saved (in Telegram or here), plus what's still being answered and a proposal
 *  waiting for a yes or no. `version` goes up when a message was just sent: reload in place. */
function Thread({ id, version, onView }: { id: string; version: number; onView: (url: string) => void }) {
  const navigate = useNavigate();
  const [result, setResult] = useState<Result<Transcript> | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [decideProblem, setDecideProblem] = useState<string | null>(null);
  const [slow, setSlow] = useState(false);
  const current = useRef(id);
  current.current = id;
  const end = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const loaded = await getConversation(id);
    if (current.current === id) setResult(loaded);  // a slow load of a conversation you left is dropped
  }, [id]);

  useEffect(() => { setResult(null); load(); }, [load]);           // another conversation: start fresh
  useEffect(() => { if (version) load(); }, [version, load]);      // just sent: reload in place

  const thread = result?.state === "ready" ? result.data : null;
  const waitingFor = thread?.waiting.map((w) => w.job_id).join(",") ?? "";

  useEffect(() => {  // ask about the answer every second (every 3 after a while) until it's in
    if (!waitingFor) return;
    const jobs = waitingFor.split(",").map(Number);
    const started = Date.now();
    let alive = true;
    let timer: number | undefined;
    let trouble: number | null = null;  // when checking started failing (e.g. a deploy restarting Sidekick)
    const check = async () => {
      const states = await Promise.all(jobs.map(jobStatus));
      if (!alive) return;
      // Can't ask right now: the message is safe in the queue, so keep asking for a while instead of showing an
      // error (Oct 28: one check during a deploy showed "error 502" though the answer came moments later).
      const unreachable = states.some((s) => s.state === "unavailable");
      trouble = unreachable ? trouble ?? Date.now() : null;
      if (unreachable && Date.now() - trouble! < GIVE_UP_MS) {
        timer = window.setTimeout(check, 3000);
        return;
      }
      if (states.some((s) => s.state !== "ready" || s.data.status !== "waiting")) {
        setSlow(false);
        await load();
        conversationsChanged();
        return;
      }
      setSlow(Date.now() - started > SLOW_MS);
      timer = window.setTimeout(check, Date.now() - started > SLOW_MS ? 3000 : 1000);
    };
    timer = window.setTimeout(check, 800);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [waitingFor, load]);

  const count = (thread?.messages.length ?? 0) + (thread?.waiting.length ?? 0);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [count, thread?.proposal?.id]);

  async function decide(kind: "approve" | "reject", actionId: number) {
    setDeciding(true);
    setDecideProblem(null);
    const outcome = await decideProposal(kind, actionId);
    if (outcome.state === "unavailable") setDecideProblem(outcome.reason);
    await load();
    setDeciding(false);
  }

  async function remove() {
    if (!window.confirm("Delete this conversation? Its messages are erased for good.")) return;
    const outcome = await deleteConversation(Number(id));
    if (outcome.state === "ready") {
      SENT.delete(Number(id));
      conversationsChanged();
      navigate("/app/chat", { replace: true });
    } else {
      setDecideProblem(outcome.state === "unavailable" ? outcome.reason : "Sign in again, then try once more.");
    }
  }

  const local = SENT.get(Number(id)) ?? [];
  const files = thread ? withFiles(thread.messages, local) : new Map<number, LocalFile>();

  if (result === null) return <div className="thread"><Skeleton lines={6} /></div>;
  if (thread === null) {
    return <EmptyState icon="history" title="Couldn't open this conversation"
      body={result.state === "unavailable" ? result.reason : "Sign in to see your conversations."} />;
  }
  return (
    <div className="thread">
      <header className="thread-head">
        <div className="thread-title">
          <h1>{thread.title}</h1>
          <button className="btn btn-ghost btn-sm" onClick={remove} aria-label="Delete this conversation"
            title="Delete this conversation"><Icon name="trash" size={16} /></button>
        </div>
        <p className="muted small">
          {thread.origin === "telegram" ? "Started in Telegram" : "Started here"} · kept for 30 days after the last message
        </p>
      </header>
      {thread.messages.map((m, i) => {
        const file = files.get(i);
        return (
          <div key={i} className={`msg ${m.role}`}>
            {file && !file.pdf && <FileView file={file} onView={onView} />}
            {(!file || file.pdf || file.text) && (
              <div className="msg-bubble">{file ? <>{file.pdf && <FileView file={file} onView={onView} />}{file.text}</> : m.text}</div>
            )}
            <span className="msg-meta">{m.channel === "telegram" ? "Telegram · " : ""}{when(m.at)}</span>
          </div>
        );
      })}
      {thread.waiting.map((w) => {
        const file = local.find((f) => f.jobId === w.job_id);
        return (
          <div key={w.job_id} className="msg user">
            {file && !file.pdf && <FileView file={file} onView={onView} />}
            {(!file || file.pdf || w.text) && (
              <div className="msg-bubble">
                {file ? file.pdf && <FileView file={file} onView={onView} /> : w.attachment && <AttachedLabel attachment={w.attachment} />}
                {w.text}
              </div>
            )}
            <span className="msg-meta">Sent</span>
          </div>
        );
      })}
      {thread.waiting.length > 0 && (
        <div className="msg assistant" role="status">
          <div className="msg-bubble thinking"><span className="dots" aria-hidden="true"><i /><i /><i /></span>
            {slow ? "Still working on it: web searches and email can take a minute." : "Thinking…"}</div>
        </div>
      )}
      {thread.recent_file && thread.waiting.length === 0 && (
        <p className="recent-file muted small">
          {thread.recent_file.type === "application/pdf" ? `📄 ${thread.recent_file.name}` : "📷 Your photo"} is kept
          until {clock(thread.recent_file.until)} so you can ask more about it, then deleted.
        </p>
      )}
      {thread.proposal && thread.waiting.length === 0 && (
        <div className="proposal" role="group" aria-label={`Waiting for your OK: ${thread.proposal.action}`}>
          <p><Icon name="info" size={16} /> Waiting for your OK: <strong>{thread.proposal.action}</strong>. Nothing
            changes until you answer.</p>
          <div className="proposal-actions">
            <button className="btn btn-primary btn-sm" disabled={deciding}
              onClick={() => decide("approve", thread.proposal!.id)}>✅ Yes</button>
            <button className="btn btn-secondary btn-sm" disabled={deciding}
              onClick={() => decide("reject", thread.proposal!.id)}>❌ No</button>
          </div>
          {decideProblem && <p className="composer-problem" role="alert">{decideProblem}</p>}
        </div>
      )}
      <div ref={end} />
    </div>
  );
}

function formatSize(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function Chat() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [version, setVersion] = useState(0);
  const [files, setFiles] = useState<Attachment[]>([]);
  const [notice, setNotice] = useState<Unavailable | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [viewing, setViewing] = useState<string | null>(null);  // a photo shown full size
  const input = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);

  useEffect(() => {  // grow the box with the text, up to a limit
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  useEffect(() => {
    if (!viewing) return;
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setViewing(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [viewing]);

  const latest = useRef(files);  // previews are browser memory: free them when leaving the page
  latest.current = files;
  useEffect(() => () => latest.current.forEach((f) => f.preview && URL.revokeObjectURL(f.preview)), []);

  function addFiles(list: FileList | File[]) {
    const picked = Array.from(list);
    const file = picked[0];
    const pdf = file?.type === "application/pdf";
    if (!file) return;
    if (files.length > 0 || picked.length > 1) {
      setProblem("One photo or PDF per message for now.");
    } else if (!pdf && !file.type.startsWith("image/")) {
      setProblem(`Not added: ${file.name} (only photos and PDFs).`);
    } else if (file.size > (pdf ? MAX_PDF_MB : MAX_PHOTO_MB) * 1024 * 1024) {
      setProblem(`Not added: ${file.name} (over ${pdf ? MAX_PDF_MB : MAX_PHOTO_MB} MB).`);
    } else {
      setFiles([{ id: `${file.name}-${file.size}-${file.lastModified}`, file, preview: URL.createObjectURL(file) }]);
      setProblem(null);
    }
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
    if ((!text.trim() && files.length === 0) || sending) return;
    setSending(true);
    setNotice(null);
    let attachment: Awaited<ReturnType<typeof prepare>> | undefined;
    try {
      attachment = files[0] ? await prepare(files[0].file) : undefined;
    } catch {
      setSending(false);
      setProblem("This photo can't be read here. Try a JPEG or PNG.");  // e.g. HEIC in a browser that can't show it
      return;
    }
    const result = await sendMessage(id ? Number(id) : null, text.trim(), attachment);
    setSending(false);
    if (result.state === "signed_out") {
      navigate("/app/signin", { replace: true });
    } else if (result.state === "unavailable") {
      setNotice(result);  // nothing was sent: keep what they wrote
    } else {
      const sent = files[0];
      if (sent && attachment) {  // keep it in this page's memory so the chat can show it (never sent anywhere else)
        const pdf = attachment.type === "application/pdf";
        const url = pdf ? sent.preview! : URL.createObjectURL(
          new Blob([Uint8Array.from(atob(attachment.data), (c) => c.charCodeAt(0))], { type: "image/jpeg" }));
        if (!pdf) URL.revokeObjectURL(sent.preview!);  // the smaller copy that was sent replaces the original
        const list = SENT.get(result.data.conversation_id) ?? [];
        SENT.set(result.data.conversation_id, [...list, { url, pdf, name: sent.file.name, text: text.trim(),
                                                          jobId: result.data.job_id }]);
      }
      setText("");
      setFiles([]);
      if (id) setVersion((v) => v + 1);  // shows the message, waiting for its answer
      else navigate(`/app/chat/${result.data.conversation_id}`);
      conversationsChanged();
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    if (e.clipboardData.files.length === 0) return;  // plain text: the box pastes it as usual
    e.preventDefault();
    addFiles(e.clipboardData.files);  // a screenshot, a copied photo or a PDF
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  }

  const canSend = (text.trim().length > 0 || files.length > 0) && !sending;

  return (
    <div className={`chat${dragging ? " dragging" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false); }}
      onDrop={onDrop}>
      <div className="chat-scroll">
        {id ? <Thread id={id} version={version} onView={setViewing} /> : <div className="chat-welcome">
          <span className="welcome-mark"><Icon name="sparkles" size={26} /></span>
          <h1>What can I help with?</h1>
          <p className="muted">
            Ask about your calendar, email, reminders and tasks, or send a photo or PDF. Your conversations,
            including the ones in Telegram, are listed on the left: open any of them to continue it here.
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
                  {a.file.type === "application/pdf" ? (
                    <a className="file-badge" href={a.preview} target="_blank" rel="noopener" aria-label={`Open ${a.file.name}`}
                      title="Open the PDF"><Icon name="file" size={18} /></a>
                  ) : (
                    <button type="button" className="attachment-open" onClick={() => setViewing(a.preview!)}
                      aria-label={`View ${a.file.name}`} title="View the photo"><img src={a.preview} alt="" /></button>
                  )}
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
            onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown} onPaste={onPaste} />
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
          Sidekick can make mistakes. Changes to your calendar or tasks always wait for your yes. Photos and PDFs
          are deleted after 10 minutes.
        </p>
      </div>

      {viewing && (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label="Photo" onClick={() => setViewing(null)}>
          <img src={viewing} alt="Your photo, full size" onClick={(e) => e.stopPropagation()} />
          <button type="button" className="icon-btn lightbox-close" onClick={() => setViewing(null)} aria-label="Close">
            <Icon name="x" />
          </button>
        </div>
      )}

      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          <Icon name="paperclip" size={28} />
          <p>Drop photos or PDFs</p>
        </div>
      )}
    </div>
  );
}
