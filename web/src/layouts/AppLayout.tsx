import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { listConversations } from "../api";
import { EmptyState, Logo, PreviewPill, Skeleton } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import ThemeSwitch from "../components/ThemeSwitch";
import { useResult } from "../useResult";

const NAV: { to: string; label: string; icon: IconName; end?: boolean }[] = [
  { to: "/app/chat", label: "Chat", icon: "chat" },
  { to: "/app", label: "Dashboard", icon: "grid", end: true },
  { to: "/app/apps", label: "Connected apps", icon: "plug" },
  { to: "/app/settings", label: "Settings", icon: "sliders" },
];

const TITLES: Record<string, string> = {
  "/app": "Dashboard", "/app/chat": "Chat", "/app/apps": "Connected apps", "/app/settings": "Settings",
};

export default function AppLayout() {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const conversations = useResult(listConversations);

  useEffect(() => setOpen(false), [pathname]);  // a tap on a link closes the drawer
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="app">
      <aside id="sidebar" className={`sidebar${open ? " open" : ""}`} aria-label="Sidebar">
        <div className="sidebar-top">
          <Link to="/" className="brand-link" aria-label="Sidekick home"><Logo size={26} /></Link>
          <button className="icon-btn mobile-only" onClick={() => setOpen(false)} aria-label="Close menu">
            <Icon name="x" />
          </button>
        </div>

        <Link to="/app/chat" className="new-chat">
          <Icon name="edit" size={18} />
          New chat
        </Link>

        <nav className="side-nav" aria-label="Main">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}
              className={({ isActive }) => `side-link${isActive ? " active" : ""}`}>
              <Icon name={item.icon} size={18} />
              {item.label}
            </NavLink>
          ))}
        </nav>

        <section className="side-section" aria-label="Recent chats">
          <p className="side-label">Recent chats</p>
          {conversations === null ? <Skeleton lines={3} /> : conversations.state === "ready" ? (
            <ul className="chat-list">
              {conversations.data.map((c) => <li key={c.id}><Link to={`/app/chat/${c.id}`}>{c.title}</Link></li>)}
            </ul>
          ) : (
            <EmptyState compact icon="history" title="No conversations yet"
              body="Your chats with Sidekick will be listed here once the web app is connected." />
          )}
        </section>

        <div className="sidebar-footer">
          <ThemeSwitch />
          <Link to="/signin" className="account-row">
            <span className="avatar"><Icon name="user" size={16} /></span>
            <span className="account-text">
              <span className="account-name">Not signed in</span>
              <span className="account-sub">Sign in with Google</span>
            </span>
            <Icon name="chevronRight" size={16} />
          </Link>
        </div>
      </aside>

      {open && <div className="scrim" onClick={() => setOpen(false)} aria-hidden="true" />}

      <div className="main">
        <header className="topbar">
          <button className="icon-btn mobile-only" onClick={() => setOpen(true)} aria-label="Open menu"
            aria-controls="sidebar" aria-expanded={open}>
            <Icon name="menu" />
          </button>
          <span className="topbar-title">{TITLES[pathname] ?? "Sidekick"}</span>
          <span className="topbar-spacer" />
          <PreviewPill />
          <Link to="/app/chat" className="icon-btn mobile-only" aria-label="New chat"><Icon name="edit" /></Link>
        </header>
        <main className="content" id="main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
