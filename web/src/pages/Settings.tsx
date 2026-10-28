import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { getPreferences, getSession, saveBriefSettings, setTelegramNotifications, signOut, type Preferences } from "../api";
import { PageHeader, PreviewPill, TelegramHint } from "../components/Common";
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

const EXAMPLES = "e.g. Job hunting for engineering roles · family visits in December · orders for my bakery";

/** When the brief comes (or off), what matters to this person, and the kinds of automated email it may use. */
function BriefSettings({ initial }: { initial: Preferences }) {
  const [on, setOn] = useState(initial.brief_time !== null);
  const [time, setTime] = useState(initial.brief_time ?? "08:00");
  const [interests, setInterests] = useState(initial.brief.interests);
  const [hidden, setHidden] = useState<string[]>(initial.brief.hidden);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    const result = await saveBriefSettings(on ? time : "off", interests, hidden);
    setSaving(false);
    setStatus(result.state === "ready" ? "Saved. Your next brief uses it."
      : result.state === "unavailable" ? result.reason : "Sign in again, then save once more.");
  }
  const toggle = (key: string, shown: boolean) =>
    setHidden((now) => (shown ? now.filter((k) => k !== key) : [...now, key]));

  return (
    <div className="brief-settings" onChange={() => setStatus(null)}>
      <Row icon="sunrise" title="Morning brief"
        body="Your day in a few lines: calendar, what needs you, and what matters to you."
        control={<label className="switch"><input type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} />
          <span aria-hidden="true" /><span className="sr-only">Send me a morning brief</span></label>} />
      {on && (
        <label className="brief-field">
          <span className="setting-title">Time</span>
          <input type="time" className="text-input" value={time} required onChange={(e) => setTime(e.target.value)} />
        </label>
      )}
      <label className="brief-field">
        <span className="setting-title">What matters to you</span>
        <span className="muted small">Its "For you" picks at most two things that match. Leave it empty and it only
          mentions something time-sensitive, like a delivery today or a bill due.</span>
        <textarea className="text-input" rows={3} maxLength={initial.brief.max_interests} placeholder={EXAMPLES}
          value={interests} onChange={(e) => setInterests(e.target.value)} />
        <span className="muted small counter">{interests.length}/{initial.brief.max_interests}</span>
      </label>
      <fieldset className="brief-field">
        <legend className="setting-title">Automated email it may use</legend>
        <span className="muted small">Emails from people always count. These only feed "For you", never "Needs
          attention".</span>
        {Object.entries(initial.brief.categories).map(([key, label]) => (
          <label key={key} className="check">
            <input type="checkbox" checked={!hidden.includes(key)} onChange={(e) => toggle(key, e.target.checked)} />
            {label}
          </label>
        ))}
      </fieldset>
      <div className="brief-save">
        <button className="btn btn-primary btn-sm" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        {status && <span className="muted small" role="status">{status}</span>}
      </div>
    </div>
  );
}

const Unavailable = ({ label }: { label: string }) => (
  <button className="btn btn-secondary btn-sm" disabled aria-disabled="true" title="Not connected yet">{label}</button>
);

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
          body={<>Signs out every browser at once. <TelegramHint>/signout_web</TelegramHint></>} />
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
        <Row icon="globe" title={`Timezone${values ? `: ${values.timezone}` : ""}`}
          body={<>So "remind me at 9" means 9 where you are. <TelegramHint>/timezone</TelegramHint></>}
          control={<Unavailable label="Change" />} />
      </Section>

      <Section title="Privacy and data">
        <Row icon="download" title="Export your data"
          body={<>A file with everything Sidekick stores about you. <TelegramHint>/export</TelegramHint></>}
          control={<Unavailable label="Export" />} />
        <Row icon="eraser" title="Delete conversations"
          body={<>Saved for 30 days. Delete the current one, or all of them.{" "}
            <TelegramHint>/forget  ·  /forget_all</TelegramHint></>}
          control={<Unavailable label="Delete" />} />
        <Row icon="trash" title="Delete account and data" danger
          body={<>Erases everything and revokes Google access. <TelegramHint>/delete_me</TelegramHint></>}
          control={<Unavailable label="Delete" />} />
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
