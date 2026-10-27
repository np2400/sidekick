import { getConnections, type Connections } from "../api";
import { PageHeader, Skeleton, TelegramHint } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import { useResult } from "../useResult";

type AppCard = {
  id: string; name: string; icon: IconName; tone: string; tagline: string;
  can: string[]; cannot: string[]; telegram: string; readOnly?: boolean;
};

const GOOGLE_APPS: AppCard[] = [
  {
    id: "calendar", name: "Google Calendar", icon: "calendar", tone: "tone-blue",
    tagline: "Your schedule, free time, and meetings, found even when a name is misspelled.",
    can: ["Read your events", "Create events, only after you approve each one"],
    cannot: ["Edit or delete your existing events"],
    telegram: "/connect",
  },
  {
    id: "gmail", name: "Gmail", icon: "mail", tone: "tone-red", readOnly: true,
    tagline: "Find bookings, summarize threads, and pull itineraries out of PDF attachments.",
    can: ["Search and read email when you ask", "Read PDF attachments (itineraries, vouchers)"],
    cannot: ["Send, change or delete any email"],
    telegram: "/connect gmail",
  },
];

/** "Connected · you@gmail.com", "Connected · server setup" or "Not connected", from the real connection. */
function status(id: string, c: Connections | null): { on: boolean; label: string } {
  if (!c) return { on: false, label: "Not connected" };
  const on = id === "calendar" ? c.google.calendar : c.google.gmail;
  const whose = c.google.managed === "server" ? "server setup" : c.google.email ?? "";
  return on ? { on, label: whose ? `Connected · ${whose}` : "Connected" } : { on, label: "Not connected" };
}

export default function Apps() {
  const connections = useResult(getConnections);
  const data = connections?.state === "ready" ? connections.data : null;

  return (
    <div className="page">
      <PageHeader title="Connected apps"
        subtitle="Sidekick only uses the accounts you connect, and asks before creating anything." />

      {connections === null ? <Skeleton /> : connections.state === "unavailable" ? (
        <div className="notice static">
          <Icon name="info" size={18} />
          <p>{connections.reason}</p>
        </div>
      ) : (
        <div className="notice static">
          <Icon name="info" size={18} />
          <p>Calendar and Gmail are separate connections: you choose each one. Connecting from the web comes in a
            later step; today use the Telegram commands below.</p>
        </div>
      )}

      <div className="apps-grid">
        {GOOGLE_APPS.map((app) => (
          <article key={app.id} className="card app-card">
            <header className="app-head">
              <span className={`app-icon ${app.tone}`}><Icon name={app.icon} size={22} /></span>
              <div className="app-title">
                <h2>{app.name}</h2>
                <div className="badges">
                  {app.readOnly && <span className="badge">Read-only</span>}
                  <span className={`badge ${status(app.id, data).on ? "badge-on" : "badge-off"}`}>
                    <span className="dot" aria-hidden="true" />{status(app.id, data).label}
                  </span>
                </div>
              </div>
            </header>
            <p className="muted">{app.tagline}</p>
            <ul className="perm-list">
              {app.can.map((c) => <li key={c} className="perm-yes"><Icon name="check" size={16} />{c}</li>)}
              {app.cannot.map((c) => <li key={c} className="perm-no"><Icon name="x" size={16} />{c}</li>)}
            </ul>
            <div className="app-actions">
              <button className="btn btn-secondary" disabled aria-disabled="true"
                title="Connecting from the web isn't available yet">
                Connect {app.name}
              </button>
              <span className="muted small">Coming soon on the web</span>
            </div>
            <TelegramHint>{app.telegram}</TelegramHint>
          </article>
        ))}

        <article className="card app-card">
          <header className="app-head">
            <span className="app-icon tone-sky"><Icon name="send" size={22} /></span>
            <div className="app-title">
              <h2>Telegram</h2>
              <div className="badges"><span className="badge badge-on"><span className="dot" aria-hidden="true" />
                {data?.telegram ? "Linked to your account" : "Available now"}</span></div>
            </div>
          </header>
          <p className="muted">
            Chat with Sidekick, get reminders with Done and Snooze buttons, and your morning brief. Invite-only: ask
            the owner for an invite link.
          </p>
          <div className="app-actions">
            <a className="btn btn-secondary" href="https://t.me/Np_Sidekick_bot" target="_blank" rel="noreferrer">
              Open in Telegram <Icon name="external" size={14} />
            </a>
          </div>
        </article>
      </div>
    </div>
  );
}
