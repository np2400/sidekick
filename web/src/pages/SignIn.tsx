import { Link } from "react-router-dom";
import { Logo } from "../components/Common";
import { GoogleG, Icon } from "../components/Icons";

export default function SignIn() {
  return (
    <main className="signin">
      <div className="card signin-card">
        <Logo size={40} withName={false} />
        <h1>Sign in to Sidekick</h1>
        <p className="muted">
          Sign in with your Google account. This only shares your name and email. Connecting Calendar or Gmail is a
          separate, optional step.
        </p>

        <button className="google-btn" disabled aria-disabled="true"
          aria-describedby="signin-status" title="Google sign-in isn't connected yet">
          <GoogleG />
          <span>Continue with Google</span>
        </button>
        <p id="signin-status" className="signin-status">
          <Icon name="info" size={16} />
          Google sign-in isn't connected yet. It arrives with the real backend.
        </p>

        <div className="divider" />
        <p className="muted small">
          Already use Sidekick on Telegram? Your web account will link to it, so both share the same reminders, tasks
          and connections.
        </p>
        <Link to="/app/chat" className="text-link">Explore the preview <Icon name="chevronRight" size={14} /></Link>
      </div>
    </main>
  );
}
