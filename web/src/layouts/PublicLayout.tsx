import { Link, Outlet } from "react-router-dom";
import { ButtonLink, Logo } from "../components/Common";
import { Icon } from "../components/Icons";
import ThemeSwitch from "../components/ThemeSwitch";

export const PRIVACY_URL = "https://neel-sidekick.duckdns.org/privacy";
// The source code, with its architecture, evaluation and deployment notes (as linked from the old home page).
export const TECH_URL = "https://github.com/np2400/sidekick";

export default function PublicLayout() {
  return (
    <div className="public">
      <header className="public-nav">
        <div className="container nav-inner">
          <Link to="/" className="brand-link" aria-label="Sidekick home"><Logo size={28} /></Link>
          <nav className="nav-links" aria-label="Sections">
            <a href="/#examples">Examples</a>
            <a href="/#privacy">Privacy</a>
            <a href={TECH_URL} target="_blank" rel="noreferrer">Source code</a>
          </nav>
          <div className="nav-actions">
            <ButtonLink to="/app/signin" variant="ghost">Sign in</ButtonLink>
            <ButtonLink to="/app">Open Sidekick</ButtonLink>
          </div>
        </div>
      </header>

      <Outlet />

      <footer className="public-footer">
        <div className="container footer-inner">
          <Logo size={22} />
          <p className="muted">Your personal AI agent, built by Neel Parekh. An invite-only beta.</p>
          <div className="footer-links">
            <a href={PRIVACY_URL} target="_blank" rel="noreferrer">Privacy policy <Icon name="external" size={14} /></a>
            <a href={TECH_URL} target="_blank" rel="noreferrer">Source code on GitHub <Icon name="external" size={14} /></a>
          </div>
          <ThemeSwitch />
        </div>
      </footer>
    </div>
  );
}
