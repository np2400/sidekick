import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { connectGoogle, disconnectGoogle, getConnections, type Connections } from "../api";
import { PageHeader, Skeleton } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import { useResult } from "../useResult";

type AppCard = {
  id: string; name: string; icon: IconName; tone: string; tagline: string;
  can: string[]; cannot: string[]; readOnly?: boolean;
};

const GOOGLE_APPS: AppCard[] = [
  {
    id: "calendar", name: "Google Calendar", icon: "calendar", tone: "tone-blue",
    tagline: "Your schedule, free time, and meetings, found even when a name is misspelled.",
    can: ["Read your events", "Create events, only after you approve each one"],
    cannot: ["Edit or delete your existing events"],
  },
  {
    id: "gmail", name: "Gmail", icon: "mail", tone: "tone-red", readOnly: true,
    tagline: "Find bookings, summarize threads, and pull itineraries out of PDF attachments.",
    can: ["Search and read email when you ask", "Read PDF attachments (itineraries, vouchers)"],
    cannot: ["Send, change or delete any email"],
  },
];

/** "Connected · you@gmail.com", "Connected · server setup", "Expired" or "Not connected", from the real connection
 * (the server asks Google whether the sign-in still works). */
function status(id: string, c: Connections | null): { on: boolean; label: string } {
  if (!c) return { on: false, label: "Not connected" };
  const granted = id === "calendar" ? c.google.calendar : c.google.gmail;
  if (granted && c.google.expired) return { on: false, label: "Expired · connect again" };
  const whose = c.google.managed === "server" ? "server setup" : c.google.email ?? "";
  return granted ? { on: true, label: whose ? `Connected · ${whose}` : "Connected" } : { on: false, label: "Not connected" };
}

const UNVERIFIED = "Google will warn that it hasn't verified Sidekick: it's a small, invite-only app. Choose " +
  "Advanced, then Go to Sidekick. You can disconnect at any time, here or in your Google account.";

export default function Apps() {
  const connections = useResult(getConnections);
  const [data, setData] = useState<Connections | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  useEffect(() => { if (connections?.state === "ready") setData(connections.data); }, [connections]);
  const connected = params.get("connected");  // back from Google's sign-in page
  const failed = params.get("problem");

  async function connect(gmail: boolean) {
    setBusy(true);
    setProblem(null);
    const result = await connectGoogle(gmail);
    if (result.state === "ready") window.location.assign(result.data.url);  // to Google, then back here
    else {
      setBusy(false);
      setProblem(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
    }
  }

  async function disconnect() {
    if (!window.confirm("Disconnect Google? Sidekick loses access to your Calendar and Gmail.")) return;
    setBusy(true);
    const result = await disconnectGoogle();
    setBusy(false);
    if (result.state === "ready") setData(result.data);
    else setProblem(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
  }

  function actions(app: AppCard) {
    if (!data) return null;
    const { on } = status(app.id, data);
    if (on && data.google.managed === "server") {
      return <span className="muted small">Connected through Sidekick's server setup (you're the owner).</span>;
    }
    if (on) {
      return <button className="btn btn-secondary" onClick={disconnect} disabled={busy}>Disconnect Google</button>;
    }
    // The owner's Sidekick always uses Calendar and Gmail together, so their one button connects both.
    const gmail = data.owner || app.id === "gmail" || (data.google.expired && data.google.gmail);
    const again = data.google.expired ? "Reconnect" : "Connect";
    return (
      <>
        <button className="btn btn-primary" onClick={() => connect(gmail)} disabled={busy}>
          {data.owner ? `${again} Calendar + Gmail` : `${again} ${app.name}`}
        </button>
        {!data.owner && app.id === "gmail" && !data.google.calendar &&
          <span className="muted small">Also connects Calendar</span>}
      </>
    );
  }

  return (
    <div className="page">
      <PageHeader title="Connected apps"
        subtitle="Sidekick only uses the accounts you connect, and asks before creating anything." />

      {(connected || failed) && (
        <div className="notice static" role="status">
          <Icon name={connected ? "check" : "info"} size={18} />
          <p>{connected ? `Connected: ${connected === "gmail" ? "Google Calendar and Gmail (read-only)" : "Google Calendar"}.`
            : failed}</p>
          <button className="btn btn-ghost btn-sm" onClick={() => setParams({}, { replace: true })}>Dismiss</button>
        </div>
      )}
      {connections === null ? <Skeleton /> : connections.state === "unavailable" ? (
        <div className="notice static">
          <Icon name="info" size={18} />
          <p>{connections.reason}</p>
        </div>
      ) : data && (data.google.managed === "you" || data.google.expired) && (
        <div className="notice static">
          <Icon name="info" size={18} />
          <p>{data.owner ? "Your Sidekick uses Calendar and Gmail (read-only) together."
            : "Calendar and Gmail are separate choices: Gmail is optional and read-only."} {UNVERIFIED}</p>
        </div>
      )}
      {problem && <div className="notice static" role="alert"><Icon name="info" size={18} /><p>{problem}</p></div>}

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
            <div className="app-actions">{actions(app)}</div>
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
