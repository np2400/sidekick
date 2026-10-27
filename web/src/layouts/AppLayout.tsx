import { useEffect, useState } from "react";
import { Link, Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { getSession, listConversations, signOut } from "../api";
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
  "/app": "Dashboard", "/app/apps": "Connected apps", "/app/settings": "Settings",
};

export default function AppLayout() {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const session = useResult(getSession);
  const conversations = useResult(listConversations);

  useEffect(() => setOpen(false), [pathname]);  // a tap on a link closes the drawer
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (session?.state === "signed_out") return <Navigate to="/app/signin" replace />;

  async function leave() {
    await signOut();
    navigate("/app/signin", { replace: true });
  }

  const me = session?.state === "ready" ? session.data : null;
  return (
    <div className="app">
      <aside id="sidebar" className={`sidebar${open ? " open" : ""}`} aria-label="Sidebar">
        <div className="sidebar-top">
          <Link to="/app" className="brand-link" aria-label="Sidekick home"><Logo size={26} /></Link>
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

        <section className="side-section" aria-label="Your conversations">
          <p className="side-label">Conversations</p>
          {conversations === null ? <Skeleton lines={3} /> : conversations.state === "ready" ? (
            conversations.data.length ? (
              <ul className="chat-list">
                {conversations.data.map((c) => (
                  <li key={c.id}>
                    <NavLink to={`/app/chat/${c.id}`} className={({ isActive }) => (isActive ? "active" : "")}
                      title={c.origin === "telegram" ? "Started in Telegram" : undefined}>
                      {c.origin === "telegram" && <Icon name="send" size={13} />}
                      <span className="chat-title">{c.title}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState compact icon="history" title="No conversations yet"
                body="Chats with Sidekick, in Telegram or here, are kept for 30 days and listed here." />
            )
          ) : conversations.state === "unavailable" ? (
            <EmptyState compact icon="history" title="Couldn't load conversations" body={conversations.reason} />
          ) : null}
        </section>

        <div className="sidebar-footer">
          <ThemeSwitch />
          <div className="account-row">
            <span className="avatar"><Icon name="user" size={16} /></span>
            <span className="account-text">
              <span className="account-name">{me ? me.name || "You" : "…"}</span>
              <span className="account-sub">{me?.google ?? (me ? "Signed in" : "")}</span>
            </span>
            {me && <button className="icon-btn small" onClick={leave} aria-label="Sign out" title="Sign out">
              <Icon name="logout" size={16} />
            </button>}
          </div>
        </div>
      </aside>

      {open && <div className="scrim" onClick={() => setOpen(false)} aria-hidden="true" />}

      <div className="main">
        <header className="topbar">
          <button className="icon-btn mobile-only" onClick={() => setOpen(true)} aria-label="Open menu"
            aria-controls="sidebar" aria-expanded={open}>
            <Icon name="menu" />
          </button>
          <span className="topbar-title">{pathname.startsWith("/app/chat") ? "Chat" : TITLES[pathname] ?? "Sidekick"}</span>
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
