import type { ReactNode } from "react";
import { getPreferences, getSession } from "../api";
import { ButtonLink, PageHeader, PreviewPill, TelegramHint } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
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
  const session = useResult(getSession);
  const prefs = useResult(getPreferences);
  const signedIn = session?.state === "ready";

  return (
    <div className="page narrow">
      <PageHeader title="Settings" subtitle="Your account, preferences and data." />

      <Section title="Account">
        <Row icon="user" title={signedIn ? session.data.name : "Not signed in"}
          body={signedIn ? session.data.email : "Sign in with Google to see your account here."}
          control={!signedIn && <ButtonLink to="/signin" variant="secondary">Sign in</ButtonLink>} />
      </Section>

      <Section title="Appearance">
        <Row icon="sun" title="Theme" body="Follow your device, or always light or dark. Saved in this browser."
          control={<ThemeSwitch showLabels />} />
      </Section>

      <Section title="Preferences">
        <Row icon="globe" title="Timezone" body={<>So "remind me at 9" means 9 where you are.{" "}
          {prefs?.state === "unavailable" && <TelegramHint>/timezone</TelegramHint>}</>}
          control={<Unavailable label="Change" />} />
        <Row icon="sunrise" title="Morning brief" body={<>A summary of your day, at a time you choose.{" "}
          {prefs?.state === "unavailable" && <TelegramHint>/brieftime 7:30</TelegramHint>}</>}
          control={<Unavailable label="Set time" />} />
      </Section>

      <Section title="Privacy and data">
        <Row icon="download" title="Export your data"
          body={<>A file with everything Sidekick stores about you. <TelegramHint>/export</TelegramHint></>}
          control={<Unavailable label="Export" />} />
        <Row icon="eraser" title="Clear recent conversation"
          body={<>Sidekick forgets your recent messages. <TelegramHint>/forget</TelegramHint></>}
          control={<Unavailable label="Clear" />} />
        <Row icon="trash" title="Delete account and data" danger
          body={<>Erases everything and revokes Google access. <TelegramHint>/delete_me</TelegramHint></>}
          control={<Unavailable label="Delete" />} />
        <a className="text-link" href={PRIVACY_URL} target="_blank" rel="noreferrer">
          Read the privacy policy <Icon name="external" size={14} />
        </a>
      </Section>

      <Section title="About">
        <Row icon="info" title="Sidekick Web" body="Version 0.1, a preview of the web app. Telegram works on its own."
          control={<PreviewPill />} />
      </Section>
    </div>
  );
}
