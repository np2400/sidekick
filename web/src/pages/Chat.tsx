import { useCallback, useEffect, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  conversationsChanged, decideProposal, getConversation, jobStatus, sendMessage, type Result, type Transcript,
  type Unavailable,
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
const MAX_MB = 10;
const UPLOADS = false;  // photos and PDFs from the web: the next step (W5b)
const UPLOADS_LATER: Unavailable = {
  state: "unavailable", reason: "Photos and PDFs from the web come in the next step, so nothing was attached.",
  today: "Send them to Sidekick in Telegram for now.",
};
const SLOW_MS = 20_000;  // after this, say that long answers (web search, email) take a while

type Attachment = { id: string; file: File; preview?: string };

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric",
                                            minute: "2-digit" });

/** One conversation, exactly as saved (in Telegram or here), plus what's still being answered and a proposal
 *  waiting for a yes or no. `version` goes up when a message was just sent: reload in place. */
function Thread({ id, version }: { id: string; version: number }) {
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
    const check = async () => {
      const states = await Promise.all(jobs.map(jobStatus));
      if (!alive) return;
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

  if (result === null) return <div className="thread"><Skeleton lines={6} /></div>;
  if (thread === null) {
    return <EmptyState icon="history" title="Couldn't open this conversation"
      body={result.state === "unavailable" ? result.reason : "Sign in to see your conversations."} />;
  }
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
      {thread.waiting.map((w) => (
        <div key={w.job_id} className="msg user">
          <div className="msg-bubble">{w.text}</div>
          <span className="msg-meta">Sent</span>
        </div>
      ))}
      {thread.waiting.length > 0 && (
        <div className="msg assistant" role="status">
          <div className="msg-bubble thinking"><span className="dots" aria-hidden="true"><i /><i /><i /></span>
            {slow ? "Still working on it: web searches and email can take a minute." : "Thinking…"}</div>
        </div>
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
    if (!UPLOADS) {
      setNotice(UPLOADS_LATER);
      return;
    }
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
    if (!text.trim() || sending) return;
    setSending(true);
    setNotice(null);
    const result = await sendMessage(id ? Number(id) : null, text.trim());
    setSending(false);
    if (result.state === "signed_out") {
      navigate("/app/signin", { replace: true });
    } else if (result.state === "unavailable") {
      setNotice(result);  // nothing was sent: keep what they wrote
    } else {
      setText("");
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

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  }

  const canSend = text.trim().length > 0 && !sending;

  return (
    <div className={`chat${dragging ? " dragging" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false); }}
      onDrop={onDrop}>
      <div className="chat-scroll">
        {id ? <Thread id={id} version={version} /> : <div className="chat-welcome">
          <span className="welcome-mark"><Icon name="sparkles" size={26} /></span>
          <h1>What can I help with?</h1>
          <p className="muted">
            Ask about your calendar, email, reminders and tasks. Your conversations, including the ones in
            Telegram, are listed on the left: open any of them to continue it here.
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
          Sidekick can make mistakes. Changes to your calendar or tasks always wait for your yes. Photos and PDFs:
          send them in Telegram for now.
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
