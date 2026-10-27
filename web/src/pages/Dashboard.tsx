import type { ReactNode } from "react";
import { getBrief, listReminders, listTasks, type Result } from "../api";
import { ButtonLink, EmptyState, PageHeader, Skeleton } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import { useResult } from "../useResult";

function greeting(now: Date) {
  const h = now.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function Panel<T>({ icon, title, result, empty, children, wide = false }: {
  icon: IconName; title: string; result: Result<T> | null; empty: string; wide?: boolean;
  children: (data: T) => ReactNode;
}) {
  return (
    <section className={`card panel${wide ? " wide" : ""}`} aria-label={title}>
      <header className="panel-head">
        <span className="panel-icon"><Icon name={icon} size={18} /></span>
        <h2>{title}</h2>
      </header>
      {result === null ? <Skeleton /> : result.state === "ready" ? children(result.data) : (
        <EmptyState icon="plug" title={empty} result={result} />
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
      <PageHeader title={greeting(now)} subtitle={`${date} · your brief, reminders and tasks in one place`}
        actions={<ButtonLink to="/app/chat" variant="secondary"><Icon name="chat" size={16} /> Ask Sidekick</ButtonLink>} />

      <div className="dash-grid">
        <Panel icon="sunrise" title="Daily brief" result={brief} empty="Your brief isn't connected yet" wide>
          {(b) => <p className="brief-text">{b.text}</p>}
        </Panel>
        <Panel icon="bell" title="Reminders" result={reminders} empty="No reminders to show">
          {(items) => (
            <ul className="rows">{items.map((r) => (
              <li key={r.id}><span>{r.text}</span><span className="muted small">{r.next}</span></li>
            ))}</ul>
          )}
        </Panel>
        <Panel icon="listChecks" title="Tasks" result={tasks} empty="No tasks to show">
          {(items) => (
            <ul className="rows">{items.map((t) => (
              <li key={t.id}><span>{t.title}</span>{t.due && <span className="muted small">{t.due}</span>}</li>
            ))}</ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
