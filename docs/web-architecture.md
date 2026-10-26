# Sidekick Web: architecture

**Status: proposal v2 (Oct 26, 2026). Nothing here is built yet.** v2 adds shared chat history, an installable
app with phone notifications, and a launch plan. Each milestone (§15) is built, tried live by the owner, and only
then followed by the next.

## 1. Goals

1. Sidekick accounts exist on their own, not as "a Telegram user".
2. Existing Telegram users can link a web sign-in to their account, proving they own both.
3. Signing in with Google is separate from giving Sidekick access to Gmail or Calendar.
4. Linked Telegram and web share one saved chat history. Each conversation is its own thread with its own
   short-term memory.
5. Sidekick installs as an app on iPhone and Android and sends real reminder and briefing notifications, even
   when it's closed.
6. One person can never reach another person's chats, history, memory, files, reminders, approvals,
   notifications or Google connections. The server enforces this, and tests try to break it.
7. Requests are processed durably: nothing is silently lost.
8. Telegram keeps working during development and can be removed later without rebuilding the agent.

**Kept as is:** the FastAPI backend, the agent loop, the model router, the tools and integrations, the evals and
the deployment.

**Not doing:**
- No Postgres, Redis/Celery, Firebase SDK, JWTs, ORM or separate auth service.
- No renaming of database columns.
- One server and SQLite (backed up by Litestream) are right for 10 to 20 users.
- **One new dependency:** `pywebpush`, for the encryption Web Push requires. It isn't something to write by hand.

## 2. Where we are today

- **Telegram is already at the edge.** Only `main.py`, `telegram.py`, `notify.py` and `set_webhook.py` talk to
  Telegram. `run_agent`, the tools, memory, reminders and the brief take a `ToolContext`, and the scheduler gets
  its `send` function passed in.
- **But every person *is* a Telegram id.** Every per-person table has a `chat_id` column holding the Telegram id.
  The owner is `TELEGRAM_OWNER_ID` in `.env`, and members are rows in `users (telegram_id)`.
- **Places that assume "account = Telegram chat"** (the full list, so W1 has a clear scope): the webhook and
  button handler in `main.py`, the Google callback's confirmation message, reminder delivery (`reminders._deliver`),
  the morning brief (`daily.py`), `notify.py` and `mcp_server.py` (owner).
- **Memory today** is one endless stream per chat, saved in `messages`:
  - Each message is trimmed to 800 characters *before* it's saved.
  - Everything is deleted after 7 days.
  - The model sees the last 8 messages from the past 2 hours.
- **At-most-once processing.** The webhook marks a Telegram update as seen, then runs the agent inside the HTTP
  request. If the container dies mid-answer (a crash, or an auto-deploy restart), the message is lost and nobody
  is told.
- **Google today:** members' tokens are stored encrypted in `google_accounts`. The owner's sign-in is a
  server-level file (`google_token.json`).

## 3. The shape: one core, two front doors, three ways out

```
 Telegram webhook ─► who? telegram:<id> ─┐                                        ┌─► in-app notification center
                                          ├─► account ─► jobs ─► worker ─► core ─► deliver ─┼─► Web Push (each device)
 Web /api ─────────► who? session cookie ─┘                     run_agent, tools,         └─► Telegram (optional)
                                                                 stores, scheduler
```

The core only knows **accounts and conversations**. Only the front doors and the deliverers know **channels**.
Removing Telegram means deleting one front door and one deliverer.

## 4. Accounts and identities

New tables:

```sql
accounts   (id INTEGER PRIMARY KEY, name, status, invited_by, created_at)        -- active | removed
identities (provider, subject, account_id, label, created_at,
            PRIMARY KEY (provider, subject), UNIQUE (account_id, provider))      -- telegram:<id>, google:<sub>
```

- **Every existing person gets an account** plus a `telegram` identity.
- **The migration only adds; it rewrites nothing** (chosen in §18.1 over rewriting ids in 13 tables).
  - Accounts that came from Telegram keep their Telegram id as their account id, so every `chat_id` column already
    holds the right account id and no existing row changes.
  - Accounts born on the web (W3 onward) get **negative** ids, which no Telegram user can have.
  - An account id never changes once it's given.
- **Code never relies on "account id = Telegram id".** A Telegram chat is always looked up in `identities`.
  - Tests use linked accounts whose id differs from their Telegram id, so a shortcut fails loudly.
  - If a stray code path ever passed a negative account id to Telegram, the send would fail: it could never reach
    a stranger.
- **`users` stays and is still written during W1 and W2,** so the previous version of Sidekick can run on the
  migrated database. Reads move to `accounts` and `identities`. `users` stops being written at W3, when web-born
  accounts appear.
- **Columns keep the name `chat_id`, now meaning "account id".** The migration says so in a comment.
- **The owner is still decided by config, never by a database row.** Today that's "the account holding
  `telegram:TELEGRAM_OWNER_ID`", and that account is created at startup if it's missing. Without Telegram it
  becomes `OWNER_GOOGLE_SUB`.

## 5. Signing in with Google is not the same as connecting Gmail or Calendar

|                 | **Sign in with Google**                     | **Connect Calendar / Gmail** (exists today)              |
|-----------------|---------------------------------------------|----------------------------------------------------------|
| Answers         | *Who are you?* (authentication)             | *What may Sidekick access?* (authorization)              |
| Scopes          | `openid email profile`                      | `calendar.events`, plus `gmail.readonly` only if chosen  |
| Tokens kept     | none: the ID token is checked, then dropped | refresh token, encrypted, in `google_accounts`           |
| Redirect        | `/auth/google/callback` (new)               | `/oauth/google/callback` (existing)                      |
| Stored          | identity `google:<sub>`, email for display  | the connection, per account                              |
| Google's review | basic scopes: light                         | Gmail is a restricted scope: "unverified app" screen     |

- **The identity key is Google's `sub`, never the email.** An email address can change or be reused by someone else.
- **Checks on sign-in:** single-use `state`, PKCE, `nonce`, and the ID token's `iss`, `aud`, `exp` and
  `email_verified`.
- **Separate single-use states.** Sign-in and connect each have their own (`login_states` vs `oauth_states`), so
  a sign-in can't be replayed as a connect, or the other way round.
- **Connecting from the web requires a matching session:** the account signed in to this browser must be the one
  that started the connect.
- **The connected Google account may differ from the sign-in account** (e.g. sign in with a personal account,
  connect a work calendar). Settings shows which is which.
- **Setup:** the same "Sidekick Web" OAuth client with one more redirect URI.

## 6. Sessions

```sql
sessions (token_hash PRIMARY KEY, account_id, created_at, expires_at, last_seen_at)
```

- **Cookie:** `__Host-sidekick`, holding 32 random bytes, set `HttpOnly; Secure; SameSite=Lax; Path=/`. Only the
  token's SHA-256 is stored, the same way invite codes are.
- **Lifetime:** expires after 30 days without use.
- **Sign-out** deletes the session *and this device's push subscription*, so a shared phone stops getting the
  previous person's reminders.
- **`/remove` and `/delete_me`** delete every session and push subscription of the account.
- **Why not JWTs:** a session has to be revocable immediately (a removed member, a deleted account). With JWTs
  that needs a server-side list anyway.
- **CSRF protection:** `SameSite=Lax`, and every state-changing `/api` call must be JSON, carry a custom header,
  and have a matching `Origin`.
- **No CORS needed:** the site and the API are on the same domain behind Caddy.

## 7. Linking Telegram and the web: proof of both

There's one building block, a **link code**:

```sql
link_codes (code_hash PRIMARY KEY, account_id, expires_at, used_at)   -- 10 minutes, single use
```

The code is created on a side where you're already signed in (proof of one account). It's redeemed on the other
side, where you also have to sign in (proof of the other). The redeeming side then asks you to confirm, showing
which account you're joining.

**A. Existing Telegram users** (the owner, your dad, today's members): start from Telegram.

1. In Telegram, send `/web`. The bot answers with a one-time button that opens the website.
2. On the website, sign in with Google.
3. The page asks: "Add Google a•••@gmail.com to Neel's Sidekick account?" with **Link** and **Cancel**.
4. On **Link**, the `google:<sub>` identity is added to the existing account. No new account is created, so
   there's nothing to merge.

**B. Web-first users adding Telegram:**

1. On the website, go to Settings → Link Telegram. This opens `t.me/<bot>?start=link_<code>`.
2. The bot asks: "Link this Telegram to the Sidekick account a•••@gmail.com?" with ✅ and ❌.

**Rules:**

- **Linking only attaches an identity to an account that already exists.** If the identity already belongs to a
  different account, linking is refused. There are no automatic merges, so there's no data-merging code to get
  wrong.
- **The confirm step is the defense against a known trick.** An attacker creates a link code from *their own*
  account and gets the victim to tap it. Without the confirm, the victim's Telegram or Google would be attached
  to the attacker's account, along with everything the victim then sends.
- **Unlinking** works from either side. The last identity can't be removed; delete the account instead.

## 8. Isolation per account

- **Who is asking comes only from the server:** the session cookie, or Telegram's verified sender. `/api` never
  accepts an account id, and the model can never set one.
- **Everything a person owns carries their account id,** and every query is scoped to it: conversations,
  messages, jobs, uploads, reminders, tasks, approvals, notifications, push subscriptions, Google connections,
  settings and sessions.
- **Item ids are checked for ownership before anything is read or changed.** For example: a conversation, a job, a
  reminder, an approval, a notification. Someone else's item gets a 404 (not a 403), so it doesn't even reveal that
  the item exists. This prevents IDOR: changing a number in the URL to reach someone else's data.
- **Uploads** (photos, PDFs) are temporary files named after the job, read only by the worker running that job, and
  deleted when the job ends. They are never served back.
- **Google tokens are per account,** encrypted with `SECRETS_KEY`. A member can never fall back to the owner's
  token (`token_for`, unchanged).
- **Every new table goes into `account.PERSONAL`,** so `/export` and account deletion cover it. The existing test
  fails if a table is forgotten.
- **A cross-account test suite:** accounts A and B. For every `/api` endpoint and every button action, B asking
  for A's items gets a 404.
  - The same suite checks that a linked account sees one history on both channels.
  - It also checks that a removed member's sessions and push subscriptions are gone.

## 9. Conversations: saved history vs. what the model sees

Two different things, kept apart on purpose:

|                | **Saved history** (what you can scroll and reopen)         | **Model context** (sent with each request)             |
|----------------|------------------------------------------------------------|--------------------------------------------------------|
| What           | every message of every conversation, full text             | the last 8 messages *of this conversation*             |
| Where          | `messages`, grouped by `conversation_id`                   | built from those rows on each request                  |
| How long       | 30 days after the conversation's last message              | that request only                                      |
| Size           | full replies                                               | each message trimmed to 800 characters                 |
| Who sees it    | the account, on web (and `/export`)                        | the model, for that one answer                         |
| You control it | delete a conversation, all history, or the account         | `/forget` / a new conversation                         |

**Tables:**

```sql
conversations (id, account_id, origin, title_encrypted, closed, created_at, last_message_at)  -- origin: telegram | web
chat_messages (id, conversation_id, account_id, role, text_encrypted, sensitive, channel, created_at, legacy_id)
```

As built (migration 010):
- **New tables, nothing changed:** `messages` is left exactly as it was, the same additive approach as W1, so the
  previous version still runs.
- **Encrypted:** text and titles are encrypted with the vault key. No key means nothing is saved; nothing is ever
  stored as plaintext.

**Rules:**

- **One thread = one memory.** The model only ever sees the last 8 messages of the conversation it's answering
  in. Other conversations, and other channels' threads, are never mixed in, so there's no giant context window.
- **Web:** "New chat" starts a thread. Picking any earlier thread in the sidebar and writing continues it, with
  that thread's memory. This includes threads that started in Telegram.
- **Telegram** is one endless chat on screen, so the server splits it into threads:
  - A Telegram message joins the thread of the previous Telegram message if that was under 2 hours ago.
  - Otherwise, or after `/new`, it starts a new thread.
  - This reproduces today's 2-hour memory window exactly, so Telegram behaves the same as now.
  - `/forget` deletes the current thread, and `/forget all` deletes every saved one.
- **Titles:** the first words of the first message ("Whats the weather in Seattle…", or "Photo"). There's no AI
  call for titles.
- **Photos and files are never kept in history.** A photo turn is saved as "[sent a photo] caption" plus Sidekick's
  reply, as today. A PDF becomes "[sent a PDF: name]".
- **Approvals belong to a conversation (W5, when the web has several threads at once).** A typed "yes" approves
  only the proposal in *that* conversation. Approval buttons (in Telegram, the web or the notification center)
  carry the item id and work from anywhere. Still exactly-once.
- **At the account level, shared by all threads:** reminders, tasks, settings, Google connections, notifications
  and, later, long-term personal memory.
- **Long-term personal memory is a later milestone, not W1 to W7.** It will be an account-level list of facts you
  can see, edit and delete, kept separate from chat history.
- **Retention:** a daily cleanup in the existing scheduler deletes conversations 30 days after their last message.
  This replaces the 7-day delete.

**Migrating today's Telegram history (no data lost):** at startup, each account's existing `messages` rows are
copied (encrypted) into conversations, split wherever there's a 2-hour gap, with `origin = telegram` and titles
from the first message.
- Each row is copied exactly once (`legacy_id`), and none are dropped.
- The old table then drains under its old 7-day rule.
- After a rollback, rows the previous version wrote are copied in at the next start, and deletions it made
  (`/forget`) are honored.
- Existing rows stay trimmed at 800 characters; that text is already gone.
- Anything older than 7 days was already deleted under the current policy.

## 10. Notifications: installable app, Web Push, notification center

**Installable app (PWA):**
- `web/public/manifest.webmanifest`: name, icons, `display: standalone`, start page `/app`.
- `web/public/sw.js`: a small hand-written service worker for `push` and `notificationclick`. No offline mode is
  needed.
- **iPhone (iOS 16.4+):** notifications only work after Share → *Add to Home Screen*, and permission can only be
  asked after a tap inside the installed app. The app guides people through both.
- **Android:** works in Chrome, installed or not.

**Per-device subscriptions:**

```sql
push_subscriptions (id, account_id, endpoint UNIQUE, p256dh, auth, device_label,
                    created_at, last_ok_at, failures, status)                 -- active | gone
```

- One row per phone or browser that allowed notifications.
- The endpoint is a secret-like URL: never logged, never sent back to the page.
- When the push service answers 404 or 410, the subscription is marked `gone`; the device was reset or permission
  was removed.
- Settings lists your devices, each with a Remove button.

**One delivery path, on the existing scheduler:**

```sql
notifications      (id, account_id, kind, title, body, ref, actions, created_at, read_at)  -- reminder | brief | system
notification_sends (notification_id, target, status, attempts, next_try_at, last_error)   -- telegram | push:<id>
```

1. **A reminder comes due** (the existing `deliveries` row, lease and all), or the morning brief's time arrives.
   `deliver(account_id, ...)` writes, in one transaction:
   - the notification (it shows in the in-app **notification center**);
   - one send row per target: each active push subscription, plus Telegram if linked and turned on.
   Then the `deliveries` row is marked sent. This is the "outbox" pattern: the reminder can't be lost between
   deciding to send and sending.
2. **A second scheduler job sends the pending rows,** with retries: 3 tries over about 10 minutes, then give up.
   The notification stays in the center either way.
   - Each push carries a TTL, so a phone that was switched off still gets a recent reminder but not a stale one.
   - It also carries a tag (the notification id), so a retry replaces itself on the phone instead of showing twice.
   - Telegram sends record the message id, as today.
3. **Done, Snooze and Tomorrow** all call `POST /api/actions/{kind}/{id}`, the same functions as today's Telegram
   buttons:
   - **In the app:** the notification center and dashboard always show all three.
   - **On Android:** the notification itself shows up to two buttons (Done, Snooze), which act without opening
     the app.
   - **On iPhone:** web notifications have no buttons, so tapping opens the reminder in the app.
4. **The morning brief** is a notification whose short body opens the full brief in the app.
5. **Telegram notifications are a per-account setting.** They stay on by default for linked users, so nothing
   changes for today's users until they turn them off.

**Privacy and setup:**
- Push contents are encrypted end to end to the device (Web Push standard). Apple's and Google's push services
  only pass them along. Reminder text shows on the lock screen, as with Telegram.
- New secret: VAPID keys. The private key goes in `.env` only, never in git or logs. Keep a copy: losing it
  silently breaks every device's subscription.

**Limits to state honestly in the app:** delivery depends on the phone. Focus modes, battery savers, or removing
permission can delay or block notifications, and iPhone needs the home-screen install. Notifications get tested on
your real iPhone and an Android phone before anyone relies on them.

## 11. Durable request processing

The problem is the at-most-once behavior from §2. The fix is an **inbox table and a worker**, using the same
lease pattern reminder deliveries already use:

```sql
jobs (id, account_id, conversation_id, channel, source_key UNIQUE, payload_json,
      status, attempts, lease_until, created_at, finished_at)          -- queued | running | done | failed
```

1. **Accept:** the front door authenticates, inserts a job and answers right away (Telegram: 200; web: 202 with
   the job id).
   - `source_key` is `telegram:<update_id>` or a web request id, so a duplicate insert is ignored. This replaces
     `processed_updates`.
2. **Work:** a worker thread claims a job with a 2-minute lease, runs the core, saves the reply into the
   conversation, delivers it and marks the job done.
   - Two jobs run at a time, but only one per account, so a "yes" is handled after the proposal it answers.
3. **Crash or deploy mid-run:** the lease expires and the job runs again. Nothing is lost (at-least-once).
4. **Retries mustn't repeat side effects:**
   - The reply is saved once per job and sent to Telegram once.
   - Reminder creation carries the job id, so a retry can't create a second reminder.
   - Approvals are already exactly-once.
   - After 3 failed attempts the job is marked failed, and the person is told to resend.
5. **Uploads:**
   - From Telegram, the job keeps the `file_id` and downloads the file again on a retry.
   - From the web, the upload is a temporary file deleted when the job ends.
6. **Getting the answer:** the web polls `GET /api/jobs/{id}`, which checks ownership like everything else.
   Streaming (SSE) can come later.
7. **Deploys:** on shutdown the worker stops claiming new jobs, and running ones get time to finish
   (`stop_grace_period` in compose).

**Why not Redis, Celery or SQS:** there's one server, and SQLite is already the source of truth and backed up. The
queue lives in the same database as the data it changes. Revisit this when there's more than one app server.

## 12. Web API (first version)

| Area          | Endpoints                                                                                        |
|---------------|--------------------------------------------------------------------------------------------------|
| Sign-in       | `GET /auth/google/start`, `GET /auth/google/callback`, `POST /api/auth/logout`, `GET /api/me`     |
| History       | `GET /api/conversations` (Telegram and web threads), `GET /api/conversations/{id}/messages`, `DELETE /api/conversations/{id}`, `DELETE /api/conversations` (all history) |
| Chat          | `POST /api/conversations` (new), `POST /api/conversations/{id}/messages` (text, photo, PDF) → 202 `{job_id}`, `GET /api/jobs/{id}` |
| Actions       | `POST /api/actions/{kind}/{id}`: approve, reject, done, snooze, tomorrow, undo                   |
| Notifications | `GET /api/notifications`, `POST /api/notifications/read`, `GET /api/push/key`, `POST /api/push/subscriptions`, `DELETE /api/push/subscriptions/{id}` |
| Dashboard     | `GET /api/reminders`, `GET /api/tasks`, `GET /api/brief`                                         |
| Google        | `GET /api/connections`, `POST /api/connections/google`, `DELETE /api/connections/google`         |
| Account       | `GET/PATCH /api/settings` (timezone, brief time, Telegram notifications), `POST /api/link/telegram`, `DELETE /api/identities/{provider}`, `GET /api/export`, `POST /api/account/delete` |

- **Caddy** serves the built site (`web/dist`) at `/` and `/app/*`, and passes `/api/*` and `/auth/*` to FastAPI.
  The site is built inside Docker during deploy, so the server doesn't need Node.
- **One fair-use limit per account,** counted across both channels together.
- **`web/src/api.ts` switches from "unavailable" to these calls.** The pages don't change.

## 13. Launch strategy

1. **Development (W1 to W6):** you and your dad are the only accounts. Telegram keeps working throughout. From W3
   on, the web app is deployed to the real domain behind invite-only sign-in, because phones need real HTTPS to
   install the app and receive notifications.
2. **Invite 10 to 20 testers (W7),** only once all of these are true:
   - notifications are proven on a real iPhone and an Android phone;
   - the cross-account test suite passes;
   - the privacy policy is updated;
   - the Google consent screen is published;
   - Groq capacity is sorted out;
   - a backup restore has been tested.
   Sign-up stays invite-only, and the existing invite codes also work as web links.
3. **Later, public access to the real agent.** FakeWorld stays an eval-only tool and never powers what users see.
   Public access needs:
   - Google's verification for Gmail (a restricted scope), or no Gmail for public users;
   - paid model capacity with per-account limits;
   - terms of use;
   - open sign-up with abuse limits.

## 14. Removing Telegram later

- **Delete:** the webhook route, `telegram.py`, `set_webhook.py`, the Telegram send target and the `telegram`
  identities.
- **Change:** the owner check moves to `OWNER_GOOGLE_SUB`.
- **Unchanged:** the agent, tools, stores, history, job queue, notifications and web app. Telegram-origin threads
  stay in history like any other.

## 15. Milestones

Each milestone is: build → the owner tries it live → explain → next. Size: S ≈ a session, M ≈ two, L ≈ three or more.

| #  | Build                                                                                         | You try                                                         | Size |
|----|-----------------------------------------------------------------------------------------------|-----------------------------------------------------------------|------|
| W1 | Accounts and identities; ids rewritten (migration 009). Telegram behaves exactly as before.   | Telegram works as usual; `/export` matches the copy from before | M    |
| W2 | Conversation threads, 30-day encrypted history, per-thread memory, `/new` and `/forget all`; Telegram history moved in (010); privacy policy updated first | Telegram behaves as before; `/new` starts a fresh thread; `/export` shows your threads | M |
| W3 | Google sign-in, sessions, `/web` linking; read-only web: **your real history (including Telegram threads)**, reminders, tasks, connections. Deployed invite-only to the real domain | Sign in on your phone, see your own Telegram conversations; your dad sees only his | L |
| W4 | Durable jobs; Telegram moves onto the queue                                                   | Restart the server mid-answer; the reply still arrives          | M    |
| W5 | **Real web chat:** new chats, continue any thread (including Telegram ones), approvals, photo and PDF upload | A real conversation on the web, continued from a Telegram thread, approved on the web | L |
| W6 | **Installable app and Web Push:** manifest, service worker, device subscriptions, notification center, outbox with retries, Done/Snooze/Tomorrow, brief notifications, Telegram notifications optional | Install on your iPhone; close the app; a reminder arrives; tap Done | L |
| W7 | Connect Calendar/Gmail from the web, link Telegram from the web, web invites, cross-account security pass, export and delete on the web, launch checklist (§13) | A new tester joins from the web only and gets reminders without Telegram | M |

**Order, and why:**
- **W1 → W2:** the database foundations come first, with Telegram unchanged, so the risky migrations happen while
  nothing else is moving.
- **W3:** the first web version is read-only, so sign-in, linking and isolation are proven on real data before
  the web can change anything.
- **W4 → W5:** chat is built on the durable queue from day one, so its API never changes.
- **W6:** notifications come before anyone is invited to rely on the web for reminders.

**Later:** long-term personal memory (account level), streaming replies, AI titles for conversations, moving the
owner's Google sign-in onto the per-account flow, renaming `chat_id` to `account_id`, and removing Telegram.

## 16. Migration risks and added complexity

| Risk | Mitigation |
|------|------------|
| **The W1 migration touches everyone's data at once.** | It only adds tables (§18.1). Each migration is one transaction. It's rehearsed on a copy restored from the S3 backup, with an automated check of every existing row and of per-account isolation. |
| **Auto-deploy rollback restores code, not data.** | W1 and W2 are both additive, so the previous code runs on the migrated database, and the rehearsal proves it. During a rollback from W2, Sidekick starts the current conversation fresh. Pause the auto-deploy timer and deploy each one by hand while watching. |
| **History goes from 7 days to 30, in full text** (replies can quote emails). The privacy policy promises 7 days. | Update the privacy policy and tell current users *before* W2 ships; that message needs your approval. The export and deletion controls cover it. Check that the server disk (EBS) is encrypted; S3 backups already are. |
| **Telegram splitting into threads** could cut a conversation in an odd place. | It uses the same 2-hour rule as today's memory, so the model sees exactly what it sees now. `/new` gives an explicit break. |
| **At-least-once jobs** could repeat a side effect. | Idempotency keys (the job id) on replies, Telegram sends and reminder creation, plus one job per account at a time. A test kills a job mid-run and checks that nothing doubles. |
| **Web Push platform limits:** iPhone needs iOS 16.4+ and the home-screen install; no buttons on iPhone notifications; phones can delay delivery; subscriptions expire silently. | Guided install and permission. Every action also works in the app. Expired subscriptions are cleaned up on 404/410. Real-device testing before invites. Telegram notifications stay available for linked users. |
| **Testing on phones needs the real domain** (HTTPS); `127.0.0.1` doesn't work from a phone. | Desktop Chrome on the laptop can test push locally. The web app is deployed invite-only from W3, which needs your approval at that point. |
| **New secrets:** VAPID private key, session cookies. | VAPID key in `.env` with a backup copy, like `SECRETS_KEY`. Session tokens are only stored hashed. |
| **Groq free-tier daily cap.** The eval run alone hit it; 10 to 20 people will too. | The router already falls back to other models. Before W7, decide on Groq's paid tier, which costs money and needs your approval. |
| **Google "Testing" mode:** at most 100 test users, and Calendar/Gmail access expires every 7 days. | Publish the consent screen before W7. Gmail keeps the "unverified app" screen until Google verifies it. |
| **More moving parts on a 1 GB server:** the site build, two workers, push sends. | The site is built in a short-lived Docker step. Workers are threads in the existing process. Memory gets checked after W4 and W6. |

## 17. Decisions

**Decided (Oct 26):**
- **Chat history:** saved history is shared across linked channels and kept 30 days, deletable per conversation or
  all at once. Memory is per thread.
- **Notifications:** an installable app with Web Push is required before anyone relies on the web for reminders.
  Telegram notifications are optional.
- **Sign-up:** invite-only for the first 10 to 20 users. Public access to the real agent comes later.
- **Telegram:** kept during development, designed to be removable.

**Still open (needed at the milestone shown):**
- **W3:** approve deploying the invite-only web app to the real domain.
- **W2:** approve the privacy-policy change and the message telling current users about it.
- **W7:** Groq paid tier, or not.
- **W7:** publish the Google consent screen.

## 18. Safeguards (added Oct 26, before W1)

### 18.1 W1: stable account ids without rewriting data

Three ways to introduce account ids were compared:

| Option | Existing rows rewritten | Previous code still runs on the migrated database | Risk of a hidden "account = Telegram" bug |
|--------|-------------------------|---------------------------------------------------|-------------------------------------------|
| A. New ids, rewrite `chat_id` in 13 tables (the v2 plan) | all of them | **no**: undoing it needs a database restore | low: such bugs fail loudly |
| B. Add an `account_id` column to every table, backfill, switch table by table | all, into new columns | yes, during the switch | medium: two columns that can disagree, double writes for weeks |
| **C. Additive (chosen):** new `accounts` and `identities` tables; Telegram-born accounts keep their Telegram id as account id; web-born accounts get negative ids | **none** | **yes** | covered by tests with linked accounts whose id ≠ their Telegram id; a negative id can never reach a Telegram user |

C changes the least. It makes a code-only rollback (what the auto-deploy already does) safe, and it's the easiest
to verify, since every existing row must come out bit-for-bit identical.

**Rehearsal on a restored copy** (on the server, so the data never leaves it). The exact commands are in the W1
runbook:

1. Restore the latest S3 backup with Litestream into a separate folder. This also proves the backup can be
   restored.
2. Run `python -m app.db.rehearse <copy>` from the new image. It works on a second copy, applies the migrations and
   checks:
   - **Unchanged:** every existing table has the same schema, the same row count and the same content (a hash of
     all rows). That's what makes a rollback safe.
   - **Accounts:** one account per person; every identity points to an existing account; no account has two
     Telegram identities.
   - **Isolation:** each account reaches exactly the rows its Telegram user owned before, table by table. The
     per-account totals add up to the whole table, so no row is shared or lost; rows that belong to nobody are
     reported.
   - **Health:** SQLite's `integrity_check` and `foreign_key_check`; running the migration a second time changes
     nothing.
   - **Output:** counts and the last 4 digits of ids only, never message text.
3. Delete the rehearsal copies.

**Rollback and restore procedure** (exact commands in `docs/w1-runbook.md`):
- **Code rollback** (the normal path): deploy the previous commit. The database stays as it is; the old code
  ignores the new tables. Tested by running the previous commit against a migrated database.
- **Rolling forward again:** while the previous version runs, it only updates `users`. So at startup the new
  version brings accounts up to date from `users` (`sync_from_legacy`): people who joined meanwhile get an
  account, and people removed or deleted meanwhile lose access, because removal wins. It changes nothing when the
  tables agree, and the rehearsal checks that they do.
- **Full restore** (only if data were damaged):
  1. Stop Sidekick and Litestream.
  2. Move `sidekick.db` and its `-wal`/`-shm` files aside.
  3. Restore from S3, or from `pre-w1.db`.
  4. Start both again.
  The restore step is the same one rehearsed in step 1.
- **Before deploying:** take a consistent copy of the live database (`pre-w1.db`, with SQLite's backup API), pause
  the auto-deploy timer, deploy by hand, verify, then resume the timer.

**Also fixed in W1:** each migration file now runs in one transaction together with its `schema_version` row.
Before, a migration that failed halfway could leave part of its changes behind.

### 18.2 W2: what to settle before keeping 30 days of chat history

- **Sensitive data at rest.** Saved replies can quote emails, PDFs and photo descriptions. Plan: encrypt the text of
  `messages` with the existing vault (`SECRETS_KEY`, the same as Google tokens).
  - A copy of the database file or a backup then doesn't reveal chats, whether or not the server disk is encrypted.
  - It's cheap at this size. The trade-off: no searching message text in SQL, which nothing needs.
  - Also check in the AWS console whether the EC2 volume is encrypted.
- **Backups.** Litestream keeps 7 days (`retention: 168h`), so deleted conversations leave the S3 backup within
  about 7 days. The privacy policy already promises that.
  - Check that the S3 bucket doesn't have *versioning* on. If it does, add a rule that expires old versions,
    otherwise "deleted" backups stay forever.
- **Privacy policy changes, published before W2 ships** (and current users told, with your approval):
  - chats kept 30 days instead of 7, in full, deletable per conversation;
  - Telegram and web share history once linked;
  - later: Google sign-in data (name, email, Google's account id), one essential sign-in cookie, device push
    subscriptions, and notifications passing through Apple's and Google's push services, encrypted.
- **Does 8 messages × 800 characters hurt accuracy?**
  - *Within one request:* no. PDFs and emails come in as tool results (up to 2,500 characters of attachments, 6
    PDF pages), not through memory.
  - *Follow-ups:* yes, in two ways.
    - A long answer (like the 5-hotel list) is cut at 800 characters, so "what's the confirmation number for the
      4th?" may not find it.
    - Anything in a PDF that the first answer didn't mention is gone. An email can be re-read with a tool, but a
      web upload is deleted after the job.
  - *Long threads:* the model only sees the last 4 exchanges, so a reference back to the start of a long thread
    fails.
  - *Why not just send more:* memory is re-sent on every model call. A request makes 1 to 6 calls of 1,200 to
    2,100 input tokens each (measured in today's evals), so +1,000 tokens of memory can mean +6,000 tokens per
    request. That's against Groq's free limit of about 8,000 per minute.
  - *Plan, measured first:*
    1. Add eval cases for each failure above.
    2. Then switch from "8 messages × 800" to a character budget: newest first, the latest reply allowed up to
       about 3,000 characters, everything within about 6,000.
    3. Decide in W5, using eval results, whether an uploaded document's text is kept with its conversation
       (encrypted, deleted with it). That's also a privacy-policy change.
  - Saved history is full text either way, so changing the budget later needs no migration.

### 18.3 W4: retries without duplicate actions

At-least-once processing means a job can run twice. The rule that keeps that safe: **a job that already changed
something is never handed back to the model.** Only jobs that did nothing but read and think are retried from the
start.

| Side effect | Where | How a retry can't repeat it |
|-------------|-------|-----------------------------|
| Reminder created, cancelled, task completed (immediate) | our database | The change and a `job_effects (job_id, seq, tool, result)` row are written in **one transaction**. On a retry, a job with effects isn't re-run: its reply is rebuilt from the recorded results ("Done: reminder set for…"). |
| Task or event proposed (needs approval) | `pending_actions` | Staging is recorded the same way. The approval itself is already exactly-once (the `pending` → `deciding` claim). |
| Approved Calendar event | Google | The event gets an id we choose (Google allows it), derived from the action id. A repeated insert gets "already exists" (409), which is treated as success. |
| Approved Todoist task (if Todoist is the task backend) | Todoist | An intent row is written *before* the call. If a crash leaves an intent without a result, it's not retried blindly: the person is told "I may have created this, please check". |
| Reply text | our database, then Telegram or web | Saved once per job (a unique job id). For Telegram, the sent message id is recorded; a crash in the instant between Telegram accepting and us recording can still repeat the *message* (never an action). The reminder sender already handles this and labels such a message "(resent)". |
| Fair-use counter | our database | Counted once per job, not per attempt. |

**Tests (part of W4):** the worker is crashed (an injected exception) at every point: after a tool ran, after the
effect was recorded, after the reply was saved, after the send. Each test then runs the worker again and checks:

- exactly one reminder, task or pending approval exists;
- at most one Calendar call per event id;
- there is one saved reply.
