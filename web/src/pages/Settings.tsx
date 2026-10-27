import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { getPreferences, getSession, setTelegramNotifications, signOut } from "../api";
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

      <Section title="Preferences">
        <Row icon="globe" title={`Timezone${values ? `: ${values.timezone}` : ""}`}
          body={<>So "remind me at 9" means 9 where you are. <TelegramHint>/timezone</TelegramHint></>}
          control={<Unavailable label="Change" />} />
        <Row icon="sunrise" title={`Morning brief${values ? `: ${values.brief_time ?? "off"}` : ""}`}
          body={<>A summary of your day, at a time you choose. <TelegramHint>/brieftime 7:30</TelegramHint></>}
          control={<Unavailable label="Set time" />} />
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
