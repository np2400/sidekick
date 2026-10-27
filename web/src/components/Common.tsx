import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Unavailable } from "../api";
import { Icon, type IconName } from "./Icons";

export function Logo({ size = 28, withName = true }: { size?: number; withName?: boolean }) {
  return (
    <span className="logo">
      <span className="logo-mark" style={{ width: size, height: size }}>
        <Icon name="sparkles" size={Math.round(size * 0.58)} />
      </span>
      {withName && <span className="logo-name">Sidekick</span>}
    </span>
  );
}

/** Shown on app pages while the web app is an early version, so nobody expects what isn't there yet. */
export function PreviewPill() {
  return (
    <span className="preview-pill" title="An early version: chat, photos, PDFs and test notifications work; reminders as notifications come next">
      <span className="dot" aria-hidden="true" />
      <span>Preview<span className="pill-extra"> · early version</span></span>
    </span>
  );
}

export function TelegramHint({ children }: { children: ReactNode }) {
  return (
    <span className="hint">
      <Icon name="send" size={14} />
      <span>
        Today on Telegram: <code>{children}</code>
      </span>
    </span>
  );
}

/** An honest "nothing to show": what this place is for, why it's empty, and what works today. */
export function EmptyState({ icon, title, body, result, action, compact = false }: {
  icon: IconName;
  title: string;
  body?: ReactNode;
  result?: Unavailable;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`empty${compact ? " compact" : ""}`}>
      <span className="empty-icon">
        <Icon name={icon} size={compact ? 18 : 22} />
      </span>
      <div className="empty-text">
        <p className="empty-title">{title}</p>
        {(body || result) && <p className="empty-body">{body ?? result?.reason}</p>}
        {result?.today && <TelegramHint>{result.today}</TelegramHint>}
        {action}
      </div>
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => <span key={i} style={{ width: `${90 - i * 18}%` }} />)}
    </div>
  );
}

export function ButtonLink({ to, children, variant = "primary", size }: {
  to: string; children: ReactNode; variant?: "primary" | "secondary" | "ghost"; size?: "lg";
}) {
  return <Link to={to} className={`btn btn-${variant}${size ? ` btn-${size}` : ""}`}>{children}</Link>;
}
