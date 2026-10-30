import { Logo } from "../components/Common";
import { GoogleG, Icon } from "../components/Icons";

/** The Google button is a plain link: the server (/auth/google/start) sends the browser to Google and back. */
export default function SignIn() {
  return (
    <main className="signin">
      <div className="card signin-card">
        <Logo size={40} withName={false} />
        <h1>Sign in to Sidekick</h1>
        <p className="muted">
          Sign in with your Google account. This only tells Sidekick who you are: no access to your email or
          calendar. Connecting Calendar or Gmail is a separate, optional step.
        </p>

        <a className="google-btn" href="/auth/google/start">
          <GoogleG />
          <span>Continue with Google</span>
        </a>

        <div className="divider" />
        <p className="muted small">
          <strong>First time here?</strong> Sidekick is invite-only: open the invite link you were sent, and sign in
          with Google there. If you already use Sidekick in Telegram, send <code>/web</code> to the bot and open the
          link it gives you; from then on this button signs you in.
        </p>
        <a className="text-link" href="https://t.me/Np_Sidekick_bot" target="_blank" rel="noreferrer">
          Open Sidekick in Telegram <Icon name="external" size={14} />
        </a>
      </div>
    </main>
  );
}
