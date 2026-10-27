import { useState, type ReactNode } from "react";
import { ButtonLink, Logo } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import { PRIVACY_URL } from "../layouts/PublicLayout";

/** Shown in place of private details (people, places, dates, booking numbers) in real conversations. */
const R = ({ w = "4em" }: { w?: string }) => (
  <span className="redact" style={{ width: w }} role="img" aria-label="redacted" />
);

type Turn = { from: "you" | "sidekick"; content: ReactNode; photo?: boolean; steps?: { icon: IconName; label: string }[] };
type Example = { id: string; tab: string; icon: IconName; source: string; turns: Turn[] };

/**
 * Real conversations with Sidekick on Telegram, copied word for word from screenshots. Nothing is written for the
 * page: the only changes are redactions of private details. Steps under a reply are what that reply actually used.
 */
const EXAMPLES: Example[] = [
  {
    id: "web", tab: "Web search", icon: "globe", source: "Telegram · Oct 24, 2026",
    turns: [
      { from: "you", content: "Whats the weather in Seattle tomorrow" },
      { from: "sidekick", steps: [{ icon: "globe", label: "Searched the web" }],
        content: "Tomorrow (Sunday, Oct 25) in Seattle is expected to be cloudy, breezy, and chilly, with highs in the low 50s and a chance of rain according to local forecasts (FOX 13 / KOMO News)." },
      { from: "you", content: "And the day after" },
      { from: "sidekick", steps: [{ icon: "history", label: "Remembered the city" }, { icon: "globe", label: "Searched the web" }],
        content: "On Monday (Oct 26), Seattle is expected to stay rainy and cloudy with gusty winds and high temperatures in the low-to-mid 50s as an active Pacific storm system continues through the region (according to local weather reports and AccuWeather)." },
    ],
  },
  {
    id: "photo", tab: "Photos", icon: "image", source: "Telegram · Oct 24, 2026",
    turns: [
      { from: "you", photo: true, content: "" },
      { from: "sidekick", steps: [{ icon: "image", label: "Read the photo" }],
        content: "The photo shows an AirPods charging case inside a protective black and bright orange case with a metal keychain ring attached. It is resting on a piece of gray fabric. There are no dates, times, or events in this photo." },
      { from: "you", content: "What is this" },
      { from: "sidekick", steps: [{ icon: "history", label: "Answered from memory" }],
        content: "It's an Apple AirPods charging case that's been placed inside a black-and-orange protective case with a metal key-chain ring attached, sitting on a gray fabric surface." },
    ],
  },
  {
    id: "email", tab: "Email & bookings", icon: "mail", source: "Telegram · Oct 25, 2026 · private details redacted",
    turns: [
      { from: "you", content: <>What hotels do I have booked for December trip including <R w="5em" /> and others</> },
      { from: "sidekick", steps: [{ icon: "mail", label: "Searched Gmail" }],
        content: (
          <>
            Here are the hotel bookings found for your December trip:
            <ol className="ex-list">
              <li><R w="6em" /> (<R w="4em" />)<br />Check-in: Dec <R w="1.4em" />, 2026 | Check-out: Dec <R w="1.4em" />, 2026<br />Status: Corporate booking requested via <R w="3em" /></li>
              <li><R w="9em" /><br />Check-in: Dec <R w="1.4em" />, 2026 | Check-out: Dec <R w="1.4em" />, 2026<br />Reservation #: <R w="7em" /></li>
              <li><R w="6em" /> Segment (Dec <R w="1.4em" /> – Dec <R w="1.4em" />, 2026)<br />No hotel booking confirmations were found in your email or calendar for these dates.</li>
            </ol>
            <span className="ex-more">… and 2 more bookings</span>
          </>
        ) },
    ],
  },
];

const CAPABILITIES: { icon: IconName; title: string; body: string }[] = [
  { icon: "globe", title: "Searches the web", body: "Current answers with the source named." },
  { icon: "file", title: "Reads photos and documents", body: "Flyers, signs, PDFs and itineraries." },
  { icon: "plug", title: "Works with your apps", body: "Gmail (read-only) and Google Calendar." },
  { icon: "bell", title: "Takes real actions", body: "Reminders, tasks and events, with your OK." },
];

function Conversation({ example }: { example: Example }) {
  return (
    <div className="ex-thread">
      {example.turns.map((t, i) => (
        <div key={i} className={`ex-turn ${t.from}`}>
          {t.from === "sidekick" && t.steps && (
            <div className="ex-steps">
              {t.steps.map((s) => <span key={s.label} className="ex-step"><Icon name={s.icon} size={13} />{s.label}</span>)}
            </div>
          )}
          {t.photo ? (
            <div className="ex-photo"><Icon name="image" size={18} /><span>Photo</span></div>
          ) : (
            <div className="ex-bubble">{t.content}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function Showcase() {
  const [active, setActive] = useState(EXAMPLES[0].id);
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
            <span className="eyebrow"><Icon name="sparkles" size={14} /> Your personal AI agent</span>
            <h1>Your personal AI agent that goes beyond answers.</h1>
            <p className="lead">
              Sidekick searches the web, reads your photos and documents, works with your Gmail and Calendar, and gets
              things done: reminders, tasks and events, always with your OK before it changes anything that isn't its own.
            </p>
            <div className="hero-actions">
              <ButtonLink to="/signin" size="lg">Sign in</ButtonLink>
              <ButtonLink to="/app/chat" variant="secondary" size="lg">Open the preview</ButtonLink>
            </div>
            <p className="muted small">Available today on Telegram (invite-only). The web app is in preview.</p>
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
                with buttons, and every new one can be undone.
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
            <strong>Private by design.</strong> Gmail access is read-only, Google sign-ins are stored encrypted, logs
            never contain what you write, and you can export or delete your data anytime.
          </p>
          <a className="text-link" href={PRIVACY_URL} target="_blank" rel="noreferrer">
            Privacy policy <Icon name="external" size={14} />
          </a>
        </div>
      </section>

      <section className="section cta">
        <div className="container cta-inner">
          <h2>Try the preview</h2>
          <p className="muted">Web sign-in and chat are coming next. Sidekick works on Telegram today.</p>
          <ButtonLink to="/app/chat" size="lg">Open the preview</ButtonLink>
        </div>
      </section>
    </main>
  );
}
