# W1 runbook: rehearse, deploy, roll back

W1 adds the `accounts` and `identities` tables (migration 009). Nothing existing is rewritten, so the previous
version of Sidekick keeps working on the migrated database. All server commands run as `ubuntu` in
`/home/ubuntu/sidekick`.

**Tested on the Mac before this runbook was written:**
- 16 W1 tests, plus the full suite (233 passing).
- A rehearsal on a copy of the local database.
- A rollback drill: the previous commit (2ba52db) ran on a migrated copy (owner chat, an invite, a new member's
  chat). Then the new version rolled forward and picked up the member who joined meanwhile.

## 0. Before pushing W1: pause automatic deploys

```bash
sudo systemctl stop sidekick-deploy.timer        # CI will still mark W1 deployable; nothing deploys by itself
systemctl list-timers | grep sidekick || echo "paused"
```

Then push W1 to `main` from the Mac and wait for CI to pass. The `production` branch moves, but the server doesn't
pull it yet.

## 1. Rehearse on a copy restored from the S3 backup

```bash
cd ~/sidekick
# 1a. The new code, built as a separate image (the running Sidekick is not touched)
rm -rf /tmp/sidekick-w1 && mkdir /tmp/sidekick-w1
git fetch -q origin production && git archive origin/production | tar -x -C /tmp/sidekick-w1
docker build -q -t sidekick:rehearsal /tmp/sidekick-w1

# 1b. Restore the latest backup from S3 into its own folder (this also proves the backup restores)
mkdir -p rehearsal && sudo chown 10001:10001 rehearsal
docker compose run --rm -v "$PWD/rehearsal:/rehearsal" litestream \
  restore -o /rehearsal/sidekick.db -integrity-check full /data/sidekick.db

# 1c. Rehearse. Only the owner's Telegram id goes in: no secrets, and the live database isn't even mounted.
docker run --rm -v "$PWD/rehearsal:/rehearsal" \
  -e TELEGRAM_OWNER_ID="$(grep '^TELEGRAM_OWNER_ID=' .env | cut -d= -f2 | tr -d '"')" \
  sidekick:rehearsal python -m app.db.rehearse /rehearsal/sidekick.db
```

**Expected:** every line `PASS`, then `REHEARSAL PASSED: safe to deploy`. The output holds counts and
last-4-digit ids only, so it's safe to paste into the chat. If anything says `FAIL`, stop: production is untouched.

```bash
# 1d. Clean up: the restored copy is a full copy of everyone's data
sudo rm -rf rehearsal /tmp/sidekick-w1 && docker image rm -f sidekick:rehearsal
```

## 2. Deploy by hand

```bash
cd ~/sidekick
# 2a. A consistent copy of the live database, taken by the running app (SQLite's backup API)
docker compose exec -T sidekick python -c "import sqlite3; s = sqlite3.connect('/data/sidekick.db'); \
d = sqlite3.connect('/data/pre-w1.db'); s.backup(d); d.close(); print('backup ok')"
ls -la data/pre-w1.db

# 2b. Deploy: the same script the timer runs. It health-checks and rolls the code back by itself on failure.
python3 deploy/deploy.py

# 2c. Verify
docker compose logs --since 10m sidekick | grep -E "Applied migration|Accounts updated|ERROR" || true
curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1:8000/health
```

**Expected:**
- `Applied migration 009_accounts.sql`.
- No `Accounts updated` line and no `ERROR`.
- `health: 200`.

Then do the hands-on checks in Telegram (the W1 exercises), and only then:

```bash
sudo systemctl start sidekick-deploy.timer       # automatic deploys back on
```

**`data/pre-w1.db` is a copy of everyone's data that `/delete_me` doesn't reach.** Delete it after 7 days at the
latest (`sudo rm ~/sidekick/data/pre-w1.db`), in line with the 7-day backup promise in the privacy policy.

## 3. If something goes wrong

**Code rollback (the normal path).** The database stays as it is; the previous version ignores the new tables.

```bash
cd ~/sidekick
git reset -q --hard 2ba52db && docker compose up -d --build     # keep the timer paused until it's fixed
```

When the fixed version is deployed later, it updates accounts at startup from anything the previous version did
meanwhile. Removals win. The log says `Accounts updated from the users table: N changes`.

**Full restore (only if data were damaged).** Ask before doing this: it discards everything since the copy.

```bash
cd ~/sidekick
docker compose stop sidekick litestream
sudo mkdir -p data/broken && sudo mv data/sidekick.db* data/broken/
sudo cp data/pre-w1.db data/sidekick.db && sudo chown 10001:10001 data/sidekick.db
git reset -q --hard 2ba52db && docker compose up -d --build
docker compose logs --tail 20 litestream    # check it's replicating again before trusting backups
```

To restore from S3 instead of `pre-w1.db`, use the step-1b command with `-o /data/sidekick.db` once the broken
file has been moved aside.
