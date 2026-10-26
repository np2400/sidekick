# W2 runbook: rehearse, deploy, roll back

W2 adds `conversations` and `chat_messages` (migration 010). Saved history is kept 30 days and encrypted with
`SECRETS_KEY`. Like W1 it's additive: the old `messages` table is left untouched, so the previous version (W1,
`b35c35e`) still runs on the migrated database. All server commands run as `ubuntu` in `/home/ubuntu/sidekick`.

**Tested on the Mac:**
- 241 tests pass.
- 6 deliberately injected bugs were each caught.
- A rollback drill on a copy of the local database: W2 chats, then W1 (the live version) runs on it, then W2 again.
  - After rolling forward, the conversation continued with all 8 earlier messages, including the ones written
    during the rollback.
  - Nothing was readable in the new tables.

## Before deploying
- **The owner approves the new privacy policy wording** (`site/privacy.html`). It goes live with the deploy, because
  Caddy serves `site/` straight from the repo.
- **The owner tells current users** in their own words: chats are now kept 30 days (was 7), encrypted;
  `/forget` deletes the current conversation and `/forget all` all of them.
- **Production must have `SECRETS_KEY` set.** It does, because Google connections already use it. Without it,
  nothing would be saved at all (never plaintext), and the log would say `Conversation not saved`.

## 0. Pause automatic deploys, then push

```bash
sudo systemctl stop sidekick-deploy.timer && echo "timer: $(systemctl is-active sidekick-deploy.timer)"
```

## 1. Rehearse on a copy restored from S3

The same commands as W1, steps 1a to 1d (`docs/w1-runbook.md`). The rehearsal encrypts the copy with a throwaway
key, so the real key never leaves `.env`.

**Expected, in addition to W1's checks:**

```
PASS  every saved message moved into a conversation of its own account, word for word: N messages
PASS  every message's conversation belongs to the same account
PASS  conversations are stored encrypted, nothing readable
PASS  moving messages in again changes nothing
REHEARSAL PASSED: safe to deploy
```

Then clean up (step 1d).

## 2. Deploy by hand

```bash
cd ~/sidekick
docker compose exec -T sidekick python -c "import sqlite3; s = sqlite3.connect('/data/sidekick.db'); \
d = sqlite3.connect('/data/pre-w2.db'); s.backup(d); d.close(); print('backup ok')"
python3 deploy/deploy.py
docker compose logs --since 10m sidekick | grep -E "Applied migration|Imported|not saved|not imported|ERROR" || true
curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1:8000/health
curl -s https://$(grep '^SIDEKICK_DOMAIN=' .env | cut -d= -f2)/privacy | grep -c "30 days"
```

**Expected:**
- `deployed <commit>`.
- `Applied migration 010_conversations.sql`.
- `Imported N earlier messages into conversations`.
- No `not saved`, `not imported` or `ERROR` lines.
- `health: 200`.
- A number of at least 1: the new privacy policy is live.

## 3. Check in Telegram

1. **Ask something just *before* step 2**, e.g. "What's the weather in Seattle tomorrow?". **Right after** the
   deploy, ask "And the day after?". It should still know it's Seattle: your earlier conversation was moved in and
   continued.
2. `/new`, then "And the day after?". This time it shouldn't know the city.
3. `/forget`. It reports how many messages it deleted (only this conversation).
4. Optional: `/export`. The file has `conversations` (with titles) and `chat_messages` (with readable text,
   because it's your own data).

Then delete the safety copy (S3 still covers the last 7 days) and turn automatic deploys back on:

```bash
sudo rm ~/sidekick/data/pre-w2.db
sudo systemctl start sidekick-deploy.timer && echo "timer: $(systemctl is-active sidekick-deploy.timer)"
```

## If something goes wrong

**Code rollback** (the database stays; W1 ignores the new tables; while rolled back, Sidekick starts the current
conversation fresh):

```bash
cd ~/sidekick && git reset -q --hard b35c35e && docker compose up -d --build   # keep the timer paused
```

When W2 is deployed again, it copies in what W1 wrote meanwhile, and honors any `/forget` sent meanwhile.

**Full restore** (only if data were damaged; ask first): as in `docs/w1-runbook.md` §3, using `pre-w2.db` and
`b35c35e`.
