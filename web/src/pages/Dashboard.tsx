import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { getBrief, listReminders, listTasks, makeBrief, type LatestBrief, type Result } from "../api";
import { ButtonLink, EmptyState, PageHeader, Skeleton } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import { useResult } from "../useResult";

function greeting(now: Date) {
  const h = now.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function Panel<T>({ icon, title, result, children, wide = false }: {
  icon: IconName; title: string; result: Result<T> | null; wide?: boolean; children: (data: T) => ReactNode;
}) {
  return (
    <section className={`card panel${wide ? " wide" : ""}`} aria-label={title}>
      <header className="panel-head">
        <span className="panel-icon"><Icon name={icon} size={18} /></span>
        <h2>{title}</h2>
      </header>
      {result === null ? <Skeleton /> : result.state === "ready" ? children(result.data) : (
        <EmptyState icon="plug" title={`Couldn't load ${title.toLowerCase()}`}
          result={result.state === "unavailable" ? result : undefined} />
      )}
    </section>
  );
}

const madeAt = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });

/** The latest brief, and "Make my brief now". Each person's own, from their own calendar, email and priorities. */
function BriefCard() {
  const loaded = useResult(getBrief);
  const [latest, setLatest] = useState<LatestBrief | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => { if (loaded?.state === "ready") setLatest(loaded.data.latest); }, [loaded]);

  async function makeNow() {
    setBusy(true);
    setProblem(null);
    const result = await makeBrief();
    setBusy(false);
    if (result.state === "ready") setLatest(result.data.latest);
    else setProblem(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
  }

  return (
    <section className="card panel wide" aria-label="Daily brief">
      <header className="panel-head">
        <span className="panel-icon"><Icon name="sunrise" size={18} /></span>
        <h2>Daily brief</h2>
        <button className="btn btn-secondary btn-sm panel-action" onClick={makeNow} disabled={busy}>
          {busy ? "Making it…" : "Make my brief now"}
        </button>
      </header>
      {loaded === null || (loaded.state === "ready" && latest === undefined) ? <Skeleton /> :
        loaded.state !== "ready" ? (
          <EmptyState icon="plug" title="Couldn't load your brief"
            result={loaded.state === "unavailable" ? loaded : undefined} />
        ) : latest ? (
          <>
            <p className="brief-text">{latest.text}</p>
            <p className="muted small brief-made">Made {madeAt(latest.made_at)}</p>
          </>
        ) : (
          <EmptyState compact icon="sunrise" title="No brief yet"
            body={<>Make one now, or choose a time and what matters to you in{" "}
              <Link className="text-link" to="/app/settings">Settings</Link>.</>} />
        )}
      {busy && <p className="muted small" role="status">Reading your calendar and email: about 10 seconds.</p>}
      {problem && <p className="muted small" role="alert">{problem}</p>}
    </section>
  );
}

export default function Dashboard() {
  const reminders = useResult(listReminders);
  const tasks = useResult(listTasks);
  const now = new Date();
  const date = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });

  return (
    <div className="page">
      <PageHeader title={greeting(now)} subtitle={`${date} · your reminders and tasks in one place`}
        actions={<ButtonLink to="/app/chat" variant="secondary"><Icon name="chat" size={16} /> Conversations</ButtonLink>} />

      <div className="dash-grid">
        <BriefCard />
        <Panel icon="bell" title="Reminders" result={reminders}>
          {(items) => items.length ? (
            <ul className="rows">{items.map((r) => (
              <li key={r.id}>
                <span>{r.text}</span>
                <span className="muted small">{r.next}{r.repeat !== "none" ? ` · ${r.repeat}` : ""}</span>
              </li>
            ))}</ul>
          ) : <EmptyState compact icon="bell" title="No upcoming reminders"
              body={<>Try in Telegram: <code>remind me to call Mom tomorrow at 10</code></>} />}
        </Panel>
        <Panel icon="listChecks" title="Tasks" result={tasks}>
          {({ tasks: items, elsewhere }) => elsewhere ? <p className="muted">{elsewhere}</p> : items.length ? (
            <ul className="rows">{items.map((t) => (
              <li key={t.id}><span>{t.title}</span>{t.due && <span className="muted small">{t.due}</span>}</li>
            ))}</ul>
          ) : <EmptyState compact icon="listChecks" title="No open tasks"
              body={<>Try in Telegram: <code>add a task to buy milk</code></>} />}
        </Panel>
      </div>
    </div>
  );
}
