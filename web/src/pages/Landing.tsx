import { useState, type ReactNode } from "react";
import { ButtonLink, Logo } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import { PRIVACY_URL } from "../layouts/PublicLayout";

type Turn = { from: "you" | "sidekick" | "note"; content?: ReactNode; photo?: string; steps?: { icon: IconName; label: string }[] };
type Example = { id: string; tab: string; icon: IconName; source: string; turns: Turn[] };

const BASE = import.meta.env.BASE_URL;  // "/app/" in the build: the page's own files live there

/**
 * Real use of Sidekick, copied from the conversations: nothing is written for the page. Long replies are condensed,
 * and "…" marks every place something was left out; nothing is added. Steps under a reply are what it actually
 * used (checked in its trace). Shown only with nothing private in them.
 */
const EXAMPLES: Example[] = [
  {
    id: "photo", tab: "Photos", icon: "image",
    source: "Sidekick Web · Oct 27, 2026 · condensed (…)",
    turns: [
      { from: "you", photo: `${BASE}examples/battery-estimate.png`, content: "How much does it cost normally for above?" },
      { from: "sidekick", steps: [{ icon: "image", label: "Read the photo" }, { icon: "globe", label: "Searched the web" }],
        content: "An estimate of $800 to $830 for a 12V battery replacement and charging system check is unusually " +
                 "high. … Unless your car is a high-end luxury model requiring an expensive OEM battery and complex " +
                 "labor, $800+ is significantly overpriced." },
      { from: "you", content: "Can you look up what this battery replacement usually costs for my exact car model 2020 mercedes e350 sedan" },
      { from: "sidekick", steps: [{ icon: "image", label: "Looked at the photo again" }, { icon: "globe", label: "Searched the web" }],
        content: (
          <>
            For a 2020 Mercedes-Benz E350 sedan, a 12V main battery replacement typically costs:
            <ul className="ex-list">
              <li>Independent European Auto Shop: $350 to $500 total</li>
              <li>Mercedes-Benz Dealership: $450 to $650 total</li>
            </ul>
            … The E350 requires an AGM (Absorbent Glass Mat) battery … Modern Mercedes models require system
            registration/coding with a scan tool so the charging system recognizes the new battery. …
            Recommendation: An independent German auto specialist will install the correct AGM battery and perform the
            required battery registration for around $350 to $500.
          </>
        ) },
    ],
  },
  {
    id: "web", tab: "Web search", icon: "globe", source: "Sidekick Web · Oct 27, 2026",
    turns: [
      { from: "note", content: "Continuing a conversation about the Seattle Mariners that started in Telegram" },
      { from: "you", content: "When is their next game ?" },
      { from: "sidekick", steps: [{ icon: "history", label: "Remembered the team" }, { icon: "globe", label: "Searched the web" },
                                  { icon: "external", label: "Read a page" }],
        content: "The Mariners' 2026 season is over\u2014they finished 76-86 and missed the postseason, so they don't " +
                 "have any upcoming games right now." },
    ],
  },
  {
    id: "calendar", tab: "Calendar", icon: "calendar",
    source: "Sidekick Web · Oct 26, 2026",
    turns: [
      { from: "you", content: "add sidekick web test to my calendar tomorrow at 3pm" },
      { from: "sidekick", steps: [{ icon: "calendar", label: "Proposed an event: nothing created yet" }],
        content: "I'll add a calendar event titled \u201csidekick web test\u201d for tomorrow, October 27, from 3pm to " +
                 "4pm PT. Should I go ahead? Reply \u201cyes\u201d to confirm or \u201cno\u201d to cancel." },
      { from: "you", content: "✅ Yes" },
      { from: "sidekick", steps: [{ icon: "calendar", label: "Added to Google Calendar" }],
        content: "✅ Added to calendar: sidekick web test (Tue Oct 27, 03:00 PM)" },
    ],
  },
];

const CAPABILITIES: { icon: IconName; title: string; body: string }[] = [
  { icon: "image", title: "Reads photos and documents", body: "Estimates, receipts, tickets and PDFs." },
  { icon: "calendar", title: "Takes real actions", body: "Reminders, tasks and calendar events, with your OK." },
  { icon: "bell", title: "Notifies your phone", body: "Add it to your home screen; reminders arrive with the app closed." },
  { icon: "globe", title: "Searches the web", body: "Current prices, news and events." },
  { icon: "plug", title: "Works with your apps", body: "Google Calendar, and Gmail read-only." },
  { icon: "history", title: "Keeps the conversation", body: "Continue a chat from Telegram on the web, or back." },
];

function Conversation({ example }: { example: Example }) {
  return (
    <div className="ex-thread">
      {example.turns.map((t, i) => t.from === "note" ? <p key={i} className="ex-context">{t.content}</p> : (
        <div key={i} className={`ex-turn ${t.from}`}>
          {t.from === "sidekick" && t.steps && (
            <div className="ex-steps">
              {t.steps.map((s) => <span key={s.label} className="ex-step"><Icon name={s.icon} size={13} />{s.label}</span>)}
            </div>
          )}
          {t.photo && <img className="ex-photo-img" src={t.photo} alt="The photo that was sent" />}
          {t.content && <div className="ex-bubble">{t.content}</div>}
        </div>
      ))}
    </div>
  );
}

function Showcase() {
  // A link can open a given example, e.g. /#example-calendar
  const linked = EXAMPLES.find((e) => window.location.hash === `#example-${e.id}`);
  const [active, setActive] = useState(linked?.id ?? EXAMPLES[0].id);
  const example = EXAMPLES.find((e) => e.id === active)!;
  return (
    <div className="showcase">
      <div className="showcase-tabs" role="tablist" aria-label="Real conversations">
        {EXAMPLES.map((e) => (
          <button key={e.id} role="tab" aria-selected={e.id === active} aria-controls="showcase-panel"
            className={e.id === active ? "active" : ""} onClick={() => setActive(e.id)}>
            <Icon name={e.icon} size={15} />{e.tab}
          </button>
        ))}
      </div>
      <div className="showcase-window" id="showcase-panel" role="tabpanel" aria-label={example.tab}>
        <div className="showcase-bar">
          <Logo size={22} />
          <span className="showcase-real"><Icon name="check" size={13} />Real conversation</span>
        </div>
        <Conversation example={example} />
        <p className="showcase-source">{example.source}</p>
      </div>
    </div>
  );
}

export default function Landing() {
  return (
    <main>
      <section className="hero" id="examples">
        <div className="container hero-inner">
          <div className="hero-copy">
            <span className="eyebrow"><Icon name="sparkles" size={14} /> Invite-only beta</span>
            <h1>Your personal AI agent that goes beyond answers.</h1>
            <p className="lead">
              Sidekick reads your photos and documents, searches the web, works with your Google Calendar and Gmail,
              and gets things done: reminders on your phone, tasks and events, always with your OK before it changes
              anything that isn't its own.
            </p>
            <div className="hero-actions">
              <ButtonLink to="/app/signin" size="lg">Sign in</ButtonLink>
              <ButtonLink to="/app" variant="secondary" size="lg">Open Sidekick</ButtonLink>
            </div>
            <p className="muted small">An invite-only beta: chat in Telegram or on the web, with reminders on your phone.</p>
          </div>
          <Showcase />
        </div>
      </section>

      <section className="section tight">
        <div className="container">
          <div className="cap-row">
            {CAPABILITIES.map((c) => (
              <div key={c.title} className="cap">
                <span className="cap-icon"><Icon name={c.icon} size={18} /></span>
                <div><p className="cap-title">{c.title}</p><p className="cap-body">{c.body}</p></div>
              </div>
            ))}
          </div>

          <div className="actions-band">
            <div className="actions-copy">
              <h2>Real actions, on your terms</h2>
              <p className="muted">
                Anything Sidekick would add to your calendar or task list waits for a tap. Your own reminders arrive
                on your phone and in Telegram, with buttons, and every new one can be undone.
              </p>
            </div>
            <div className="actions-ui" aria-label="Product UI">
              <div className="ui-card">
                <span className="ui-label">Approval</span>
                <div className="ui-buttons"><span className="ui-btn">✅ Yes</span><span className="ui-btn">❌ No</span></div>
              </div>
              <div className="ui-card">
                <span className="ui-label">Reminder</span>
                <div className="ui-buttons">
                  <span className="ui-btn">Done</span><span className="ui-btn">Snooze 1h</span><span className="ui-btn">Tomorrow</span>
                </div>
              </div>
              <p className="ui-caption">Product UI · buttons as they appear in Sidekick</p>
            </div>
          </div>

          <ol className="flow" aria-label="How it works">
            <li><span className="flow-num">1</span>Ask in plain words</li>
            <li><span className="flow-num">2</span>Sidekick uses the right tools</li>
            <li><span className="flow-num">3</span>You approve what it creates</li>
          </ol>
        </div>
      </section>

      <section id="privacy" className="privacy-line">
        <div className="container privacy-inner">
          <Icon name="shield" size={20} />
          <p>
            <strong>Private by design.</strong> Gmail access is read-only, Google sign-ins and your conversations are
            stored encrypted, photos and PDFs are deleted after 10 minutes, logs never contain what you write, and you
            can export or delete your data anytime.
          </p>
          <a className="text-link" href={PRIVACY_URL} target="_blank" rel="noreferrer">
            Privacy policy <Icon name="external" size={14} />
          </a>
        </div>
      </section>

      <section className="section cta">
        <div className="container cta-inner">
          <h2>Invite-only beta</h2>
          <p className="muted">
            Sidekick isn't open for sign-ups yet. Invited? Sign in with the Google account you linked: send /web to
            Sidekick in Telegram to link one.
          </p>
          <ButtonLink to="/app/signin" size="lg">Sign in</ButtonLink>
        </div>
      </section>
    </main>
  );
}
