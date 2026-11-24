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
          Sidekick only gets your email address from Google, to know it's you. It can't see your email or calendar
          unless you choose to connect them later, on the Apps page.
        </p>

        <a className="google-btn" href="/auth/google/start">
          <GoogleG />
          <span>Continue with Google</span>
        </a>

        <div className="divider" />
        <p className="muted small">
          <strong>New here?</strong> Sidekick is invite-only. Open the invite link you were sent and continue with
          Google there. No Telegram needed.
        </p>
        <p className="muted small">
          Already use Sidekick in Telegram? Send <code>/web</code> to the bot to use the same account here.{" "}
          <a className="text-link" href="https://t.me/Np_Sidekick_bot" target="_blank" rel="noreferrer">
            Open Telegram <Icon name="external" size={14} />
          </a>
        </p>
      </div>
    </main>
  );
}
