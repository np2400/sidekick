# Sidekick Web (preview)

A web interface for Sidekick: landing page, sign-in, chat, dashboard, connected apps and settings. Vite + React +
TypeScript, hand-written CSS with light and dark mode, responsive down to phone width.

**Status (W3b): your real data, read-only, at `https://<domain>/app`** behind Google sign-in (linked from
Telegram with `/web`). Sending messages from the web comes next (W5). The landing page (`/`) is local-only until
it's updated: the domain's home page stays the simple one in `site/`. Telegram keeps working on its own.

## Run it

```bash
cd web
npm install        # first time only
npm run dev        # then open http://127.0.0.1:5173
```

Add `?theme=dark` or `?theme=light` to any URL to view it in that theme. `npm run build` type-checks and builds.

## The rule: no fake data

Every page gets data only through `src/api.ts`, which calls Sidekick's API (`app/web_api.py`) with the session
cookie. Each call answers with real data, "signed out" (the app goes to sign-in), or an honest "unavailable"
with the reason and, where true, how to do it in Telegram. Nothing is invented, and the composer never simulates
an answer.

## How it's served

`deploy/deploy.py` builds `web/` into `web/dist` in a throwaway Node container whenever `web/` changes, and
Caddy serves it at `/app` (`caddy/Caddyfile`). CI type-checks and builds it on every push.

## Layout

- `src/api.ts`: the one place data comes from (`/api/*`, read-only)
- `src/layouts/`: `PublicLayout` (landing, sign-in) and `AppLayout` (sidebar; a drawer on phones)
- `src/pages/`: Landing, SignIn, Chat, Dashboard, Apps, Settings, NotFound
- `src/components/`: icons (inline SVG), empty states, preview pill, theme switch
- `src/styles.css`: design tokens (light/dark), components, responsive rules

## Next: connecting the real backend

A FastAPI `/api` router beside the Telegram webhook, Google sign-in with an HttpOnly session cookie, and linking a
web account to the user's Telegram account so both share data. Endpoints reuse existing code: reminders, built-in
tasks, the brief, `run_agent` with memory for chat, photo and PDF uploads, Google connections, settings, and
`/export`/`/delete_me`. The built site is then served by Caddy on the same domain.
