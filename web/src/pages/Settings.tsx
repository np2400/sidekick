import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { conversationsChanged, createInvite, deleteAccount, deleteAllConversations, deviceTimezone, EXPORT_URL,
  getMembers, getPreferences, getSession, removeMember, saveBriefSettings, setTelegramNotifications, setTimezone,
  signOut, signOutEverywhere, type Member, type Preferences } from "../api";
import { PageHeader, PreviewPill } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import Notifications from "../components/Notifications";
import ThemeSwitch from "../components/ThemeSwitch";
import { PRIVACY_URL } from "../layouts/PublicLayout";
import { useResult } from "../useResult";

function Row({ icon, title, body, control, danger = false }: {
  icon: IconName; title: string; body: ReactNode; control?: ReactNode; danger?: boolean;
}) {
  return (
    <div className={`setting-row${danger ? " danger" : ""}`}>
      <span className="setting-icon"><Icon name={icon} size={18} /></span>
      <div className="setting-text">
        <p className="setting-title">{title}</p>
        <div className="muted small">{body}</div>
      </div>
      {control && <div className="setting-control">{control}</div>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card settings-section" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

const EXAMPLES = "e.g. Job hunting for engineering roles · my kids' school events · orders for my bakery";

/** When the brief comes (or off), and what else matters to this person. The everyday essentials are always on. */
function BriefSettings({ initial }: { initial: Preferences }) {
  const [on, setOn] = useState(initial.brief_time !== null);
  const [time, setTime] = useState(initial.brief_time ?? "08:00");
  const [interests, setInterests] = useState(initial.brief.interests);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    const result = await saveBriefSettings(on ? time : "off", interests);
    setSaving(false);
    setStatus(result.state === "ready" ? "Saved. Your next brief uses it."
      : result.state === "unavailable" ? result.reason : "Sign in again, then save once more.");
  }

  return (
    <div className="brief-settings" onChange={() => setStatus(null)}>
      <Row icon="sunrise" title="Morning brief"
        body="Your day in a few lines: your calendar, tasks and reminders, what needs you, and the next 3 days. Each brief checks your email for appointments, travel, deliveries, bills and payments."
        control={<label className="switch"><input type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} />
          <span aria-hidden="true" /><span className="sr-only">Send me a morning brief</span></label>} />
      {on && (
        <label className="brief-field">
          <span className="setting-title">Time</span>
          <input type="time" className="text-input" value={time} required onChange={(e) => setTime(e.target.value)} />
        </label>
      )}
      <label className="brief-field">
        <span className="setting-title">Anything else that matters to you (optional)</span>
        <textarea className="text-input" rows={3} maxLength={initial.brief.max_interests} placeholder={EXAMPLES}
          value={interests} onChange={(e) => setInterests(e.target.value)} />
        <span className="muted small counter">{interests.length}/{initial.brief.max_interests}</span>
      </label>
      <div className="brief-save">
        <button className="btn btn-primary btn-sm" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        {status && <span className="muted small" role="status">{status}</span>}
      </div>
    </div>
  );
}

/** The timezone Sidekick uses for this person, and this device's, if it's different. */
function TimezoneRow({ initial }: { initial: string }) {
  const [zone, setZone] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const here = deviceTimezone();

  async function useHere() {
    const result = await setTimezone(here);
    if (result.state === "ready") setZone(result.data.timezone);
    else setProblem(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
  }

  return (
    <Row icon="globe" title={`Timezone: ${zone}`}
      body={problem ?? (zone === here ? "So \"remind me at 9\" means 9 where you are. It matches this device."
        : `So "remind me at 9" means 9 where you are. This device is set to ${here}.`)}
      control={zone !== here && <button className="btn btn-secondary btn-sm" onClick={useHere}>Use {here}</button>} />
  );
}

const lastSeen = (iso: string | null) => (iso ? new Date(iso.replace(" ", "T") + "Z").toLocaleDateString(undefined,
  { month: "short", day: "numeric" }) : "never");

/** For the owner: a one-time invite link, and who's here (activity and connections, never what anyone wrote). */
function Members() {
  const loaded = useResult(getMembers);
  const [members, setMembers] = useState<Member[] | null>(null);
  const [invite, setInvite] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  useEffect(() => { if (loaded?.state === "ready") setMembers(loaded.data); }, [loaded]);

  async function newInvite() {
    const result = await createInvite();
    if (result.state === "ready") {
      setInvite(result.data.url);
      setStatus(`Works once, for one person, for ${result.data.hours} hours. They sign in with Google; no Telegram needed.`);
    } else setStatus(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(invite!);
      setStatus("Copied. Send it to the person you're inviting.");
    } catch {
      setStatus("Couldn't copy: select the link and copy it.");
    }
  }

  async function remove(m: Member) {
    if (!window.confirm(`Remove ${m.name || "this member"}? They're signed out everywhere, their reminders stop and `
      + "their Google access is revoked. Their data stays until they or you delete it.")) return;
    const result = await removeMember(m.id);
    if (result.state === "ready") setMembers(result.data);
    else setStatus(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
  }

  return (
    <>
      <Row icon="users" title="Invite someone" body="A one-time link to join Sidekick with Google."
        control={<button className="btn btn-secondary btn-sm" onClick={newInvite}>Create invite link</button>} />
      {invite && (
        <div className="danger-confirm">
          <input className="text-input" readOnly value={invite} aria-label="Invite link"
            onFocus={(e) => e.target.select()} />
          <button className="btn btn-secondary btn-sm" onClick={copy}><Icon name="copy" size={14} /> Copy</button>
        </div>
      )}
      {status && <p className="muted small brief-field" role="status">{status}</p>}
      {members?.map((m) => (
        <Row key={m.id} icon="user" title={`${m.name || "Unnamed"}${m.status === "removed" ? " (removed)" : ""}`}
          body={`${m.logins.join(" + ") || "no login"} · joined ${lastSeen(m.joined)} · last active ${lastSeen(m.last_active)} · `
            + `${m.requests_7d} requests in 7 days${m.calendar ? " · Calendar" : ""}${m.gmail ? " · Gmail" : ""}`}
          control={m.status === "active" && (
            <button className="btn btn-secondary btn-sm" onClick={() => remove(m)}>Remove</button>)} />
      ))}
      {members?.length === 0 && <p className="muted small brief-field">No members yet.</p>}
    </>
  );
}

const CONFIRM = "delete everything";

/** Export, delete all conversations, delete the account: the same as /export, /forget_all and /delete_me. */
function DataControls({ owner, onGone }: { owner: boolean; onGone: () => void }) {
  const [status, setStatus] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  async function forgetAll() {
    if (!window.confirm("Delete all your saved conversations, here and in Telegram? This can't be undone.")) return;
    const result = await deleteAllConversations();
    if (result.state === "ready") {
      conversationsChanged();
      setStatus(`Deleted ${result.data.deleted} messages.`);
    } else setStatus(result.state === "unavailable" ? result.reason : "Sign in again, then try once more.");
  }

  async function eraseAll() {
    setBusy(true);
    const result = await deleteAccount();
    setBusy(false);
    if (result.state === "ready" || result.state === "signed_out") onGone();
    else setStatus(result.reason);
  }

  return (
    <>
      <Row icon="download" title="Export your data"
        body="A file with everything Sidekick stores about you (sign-in tokens left out)."
        control={<a className="btn btn-secondary btn-sm" href={EXPORT_URL} download>Export</a>} />
      <Row icon="eraser" title="Delete all conversations"
        body="Saved for 30 days. Delete one from its page in Chat, or all of them here."
        control={<button className="btn btn-secondary btn-sm" onClick={forgetAll}>Delete all</button>} />
      <Row icon="trash" title="Delete account and data" danger
        body={owner ? "Erases everything Sidekick stores about you and revokes Google access. You can keep using Sidekick, starting empty."
          : "Erases everything and revokes Google access. You leave Sidekick; coming back needs a new invite."}
        control={!asking && <button className="btn btn-secondary btn-sm" onClick={() => setAsking(true)}>Delete…</button>} />
      {asking && (
        <div className="danger-confirm">
          <input className="text-input" value={typed} onChange={(e) => setTyped(e.target.value)}
            placeholder={`Type "${CONFIRM}" to confirm`} aria-label={`Type ${CONFIRM} to confirm`} />
          <button className="btn btn-danger btn-sm" disabled={typed.trim().toLowerCase() !== CONFIRM || busy}
            onClick={eraseAll}>{busy ? "Deleting…" : "Delete for good"}</button>
          <button className="btn btn-ghost btn-sm" onClick={() => { setAsking(false); setTyped(""); }}>Cancel</button>
        </div>
      )}
      {status && <p className="muted small brief-field" role="status">{status}</p>}
    </>
  );
}

export default function Settings() {
  const navigate = useNavigate();
  const session = useResult(getSession);
  const prefs = useResult(getPreferences);
  const me = session?.state === "ready" ? session.data : null;
  const values = prefs?.state === "ready" ? prefs.data : null;
  const [telegramOn, setTelegramOn] = useState<boolean | null>(null);
  useEffect(() => { if (values) setTelegramOn(values.telegram_notifications); }, [values]);

  async function toggleTelegram(on: boolean) {
    setTelegramOn(on);
    const result = await setTelegramNotifications(on);
    if (result.state === "ready") setTelegramOn(result.data.telegram_notifications);
    else setTelegramOn(!on);  // didn't save: show what's really set
  }

  async function leave() {
    await signOut();
    navigate("/app/signin", { replace: true });
  }

  return (
    <div className="page narrow">
      <PageHeader title="Settings" subtitle="Your account, preferences and data." />

      <Section title="Account">
        <Row icon="user" title={me ? me.name || "You" : "…"}
          body={me ? <>Signed in with Google{me.google ? ` (${me.google})` : ""}
            {me.telegram ? " · linked to your Telegram" : ""}{me.role === "owner" ? " · owner" : ""}</> : ""}
          control={me && <button className="btn btn-secondary btn-sm" onClick={leave}>Sign out</button>} />
        <Row icon="logout" title="Sign out everywhere"
          body="Signs out every browser and phone at once, this one included."
          control={<button className="btn btn-secondary btn-sm" onClick={async () => {
            if (!window.confirm("Sign out of Sidekick Web on every device, this one included?")) return;
            await signOutEverywhere();
            navigate("/app/signin", { replace: true });
          }}>Sign out everywhere</button>} />
      </Section>

      <Section title="Notifications">
        <Notifications />
        {values?.telegram && telegramOn !== null && (
          <Row icon="send" title="Also send to Telegram"
            body="Reminders and your morning brief also arrive in Telegram. Turned off, they still go there while no device here gets notifications, so nothing gets lost."
            control={<label className="switch"><input type="checkbox" checked={telegramOn}
              onChange={(e) => toggleTelegram(e.target.checked)} /><span aria-hidden="true" />
              <span className="sr-only">Also send to Telegram</span></label>} />
        )}
      </Section>

      <Section title="Appearance">
        <Row icon="sun" title="Theme" body="Follow your device, or always light or dark. Saved in this browser."
          control={<ThemeSwitch showLabels />} />
      </Section>

      <Section title="Morning brief">
        {values ? <BriefSettings initial={values} /> : <p className="muted small brief-field">Loading…</p>}
      </Section>

      <Section title="Preferences">
        {values ? <TimezoneRow initial={values.timezone} /> : <p className="muted small brief-field">Loading…</p>}
      </Section>

      {me?.role === "owner" && (
        <Section title="Members">
          <Members />
        </Section>
      )}

      <Section title="Privacy and data">
        <DataControls owner={me?.role === "owner"} onGone={() => navigate("/", { replace: true })} />
        <a className="text-link" href={PRIVACY_URL} target="_blank" rel="noreferrer">
          Read the privacy policy <Icon name="external" size={14} />
        </a>
      </Section>

      <Section title="About">
        <Row icon="info" title="Sidekick Web" body="Version 0.5: chat with Sidekick here or in Telegram, in the same conversations; reminders and your morning brief as notifications."
          control={<PreviewPill />} />
      </Section>
    </div>
  );
}
