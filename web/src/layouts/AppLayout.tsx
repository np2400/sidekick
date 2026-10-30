import { useCallback, useEffect, useState } from "react";
import { Link, Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { CONVERSATIONS_CHANGED, deviceTimezone, getNotifications, getPreferences, getSession, listConversations,
  setTimezone, signOut } from "../api";
import { NOTIFICATIONS_READ } from "../pages/NotificationsPage";
import { EmptyState, Logo, PreviewPill, Skeleton } from "../components/Common";
import { Icon, type IconName } from "../components/Icons";
import ThemeSwitch from "../components/ThemeSwitch";
import { useResult } from "../useResult";

const NAV: { to: string; label: string; icon: IconName; end?: boolean }[] = [
  { to: "/app/chat", label: "Chat", icon: "chat" },
  { to: "/app", label: "Dashboard", icon: "grid", end: true },
  { to: "/app/notifications", label: "Notifications", icon: "bell" },
  { to: "/app/apps", label: "Connected apps", icon: "plug" },
  { to: "/app/settings", label: "Settings", icon: "sliders" },
];

const TITLES: Record<string, string> = {
  "/app": "Dashboard", "/app/apps": "Connected apps", "/app/settings": "Settings", "/app/notifications": "Notifications",
};

export default function AppLayout() {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const session = useResult(getSession);
  const [listVersion, setListVersion] = useState(0);  // bumped when a chat starts or gets an answer
  useEffect(() => {
    const changed = () => setListVersion((v) => v + 1);
    window.addEventListener(CONVERSATIONS_CHANGED, changed);
    return () => window.removeEventListener(CONVERSATIONS_CHANGED, changed);
  }, []);
  const loadConversations = useCallback(() => listConversations(), [listVersion]);
  const conversations = useResult(loadConversations);
  const [unread, setUnread] = useState(0);  // new notifications, for the sidebar's count
  useEffect(() => {
    let alive = true;
    const count = () => getNotifications().then((r) => alive && setUnread(r.state === "ready" ? r.data.unread : 0));
    count();  // on every page change, and when the notifications page marks them read
    window.addEventListener(NOTIFICATIONS_READ, count);
    return () => { alive = false; window.removeEventListener(NOTIFICATIONS_READ, count); };
  }, [pathname]);

  useEffect(() => {  // someone who joined on the web never sent /timezone: use this device's, once
    getPreferences().then((r) => { if (r.state === "ready" && !r.data.timezone_set) setTimezone(deviceTimezone()); });
  }, []);

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
  const latest = conversations?.state === "ready" ? conversations.data[0] : undefined;
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
          {NAV.map((item) => {
            // "Chat" goes back to your latest conversation; only "New chat" starts a new one.
            const chat = item.to === "/app/chat";
            return (
              <NavLink key={item.to} to={chat && latest ? `/app/chat/${latest.id}` : item.to} end={item.end}
                className={({ isActive }) =>
                  `side-link${(chat ? pathname.startsWith("/app/chat/") : isActive) ? " active" : ""}`}>
                <Icon name={item.icon} size={18} />
                {item.label}
                {item.to === "/app/notifications" && unread > 0 && (
                  <span className="nav-count" aria-label={`${unread} new`}>{unread > 99 ? "99+" : unread}</span>
                )}
              </NavLink>
            );
          })}
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
