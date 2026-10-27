# Sidekick Web (preview)

A web interface for Sidekick: landing page, sign-in, chat, dashboard, connected apps and settings. Vite + React +
TypeScript, hand-written CSS with light and dark mode, responsive down to phone width.

**Status: UI preview, local only.** It is not deployed and not connected to the Sidekick backend. Telegram is
unaffected and keeps working on its own.

## Run it

```bash
cd web
npm install        # first time only
npm run dev        # then open http://127.0.0.1:5173
```

Add `?theme=dark` or `?theme=light` to any URL to view it in that theme. `npm run build` type-checks and builds.

## The rule: no fake data

Every page gets data only through `src/api.ts`. Until the backend API exists, each function there returns
`{ state: "unavailable", reason, today? }`, and pages render an honest empty state, plus how to do the same
thing on Telegram today. No conversations, tasks, reminders or replies are invented, and the composer never
simulates an answer. Google sign-in, Gmail and Calendar are shown but not active.

## Layout

- `src/api.ts`: the data seam (today: "unavailable"; next: real `/api/*` calls)
- `src/layouts/`: `PublicLayout` (landing, sign-in) and `AppLayout` (sidebar; a drawer on phones)
- `src/pages/`: Landing, SignIn, Chat, Dashboard, Apps, Settings, NotFound
- `src/components/`: icons (inline SVG), empty states, preview pill, theme switch
- `src/styles.css`: design tokens (light/dark), components, responsive rules

## Next: connecting the real backend

A FastAPI `/api` router beside the Telegram webhook, Google sign-in with an HttpOnly session cookie, and linking a
web account to the user's Telegram account so both share data. Endpoints reuse existing code: reminders, built-in
tasks, the brief, `run_agent` with memory for chat, photo and PDF uploads, Google connections, settings, and
`/export`/`/delete_me`. The built site is then served by Caddy on the same domain.
