import { useCallback, useEffect, useState } from "react";
import { getNotifications, readNotifications, reminderAction, type NoticeList } from "../api";
import { EmptyState, PageHeader, Skeleton } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";

/** The notification center (W6b): reminders and morning briefs, also the ones a phone showed. Opening it marks
 *  them read. A reminder that's still active gets Done, 1 hour and Tomorrow, like its buttons in Telegram. */

export const NOTIFICATIONS_READ = "sidekick:notifications-read";  // the sidebar's count listens for this

const ICONS: Record<string, IconName> = { reminder: "bell", brief: "sunrise", system: "info" };

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric",
                                            minute: "2-digit" });

export default function NotificationsPage() {
  const [list, setList] = useState<NoticeList | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [replies, setReplies] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    const result = await getNotifications();
    if (result.state === "ready") {
      setList(result.data);
      if (result.data.unread > 0) {
        await readNotifications();
        window.dispatchEvent(new Event(NOTIFICATIONS_READ));
      }
    } else if (result.state === "unavailable") {
      setProblem(result.reason);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function act(noticeId: number, kind: "done" | "snooze" | "tomorrow", reminderId: number) {
    setBusy(noticeId);
    const result = await reminderAction(kind, reminderId);
    setBusy(null);
    setReplies((current) => ({ ...current, [noticeId]: result.state === "ready" ? result.data.reply
      : result.state === "unavailable" ? result.reason : "Sign in again, then try once more." }));
    if (result.state === "ready") await load();  // e.g. Done: its buttons go away
  }

  return (
    <div className="page narrow">
      <PageHeader title="Notifications" subtitle="Your reminders and morning briefs, kept here for 30 days." />
      {problem && <EmptyState icon="bell" title="Couldn't load notifications" body={problem} />}
      {!problem && list === null && <div className="card"><Skeleton lines={4} /></div>}
      {list && list.items.length === 0 && (
        <EmptyState icon="bell" title="Nothing yet"
          body="When a reminder comes due or your morning brief is ready, it shows up here (and on your phone, if you turned notifications on in Settings)." />
      )}
      {list && list.items.length > 0 && (
        <ul className="notice-list">
          {list.items.map((n) => (
            <li key={n.id} className={`card notice${n.read ? "" : " unread"}`}>
              <span className="notice-icon"><Icon name={ICONS[n.kind] ?? "info"} size={18} /></span>
              <div className="notice-text">
                <p className="notice-title">{n.title}<span className="muted small"> · {when(n.at)}</span></p>
                <p className="notice-body">{n.body}</p>
                {n.reminder_id !== null && (
                  <div className="notice-actions">
                    <button className="btn btn-primary btn-sm" disabled={busy === n.id}
                      onClick={() => act(n.id, "done", n.reminder_id!)}>✅ Done</button>
                    <button className="btn btn-secondary btn-sm" disabled={busy === n.id}
                      onClick={() => act(n.id, "snooze", n.reminder_id!)}>⏰ 1 hour</button>
                    <button className="btn btn-secondary btn-sm" disabled={busy === n.id}
                      onClick={() => act(n.id, "tomorrow", n.reminder_id!)}>📅 Tomorrow</button>
                  </div>
                )}
                {replies[n.id] && <p className="muted small" role="status">{replies[n.id]}</p>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
