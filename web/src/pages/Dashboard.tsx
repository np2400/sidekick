import type { ReactNode } from "react";
import { getBrief, listReminders, listTasks, type Result } from "../api";
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

export default function Dashboard() {
  const brief = useResult(getBrief);
  const reminders = useResult(listReminders);
  const tasks = useResult(listTasks);
  const now = new Date();
  const date = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });

  return (
    <div className="page">
      <PageHeader title={greeting(now)} subtitle={`${date} · your reminders and tasks in one place`}
        actions={<ButtonLink to="/app/chat" variant="secondary"><Icon name="chat" size={16} /> Conversations</ButtonLink>} />

      <div className="dash-grid">
        <section className="card panel wide" aria-label="Daily brief">
          <header className="panel-head">
            <span className="panel-icon"><Icon name="sunrise" size={18} /></span>
            <h2>Daily brief</h2>
          </header>
          {brief === null ? <Skeleton /> : brief.state === "ready" ? <p className="brief-text">{brief.data.text}</p> : (
            <EmptyState icon="sunrise" title="Your brief comes to the web later"
              result={brief.state === "unavailable" ? brief : undefined} />
          )}
        </section>
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
