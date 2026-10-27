"""Pull-based continuous deployment, run on the server every 5 minutes by a systemd timer.

CI moves the `production` branch to a commit only after the tests and the secret scan pass on main. This script
notices when `production` moves, deploys that commit, checks it (see healthy), and rolls back if it fails.
Nothing outside reaches in: the server only pulls (no SSH from GitHub, no server keys stored in GitHub).

Standard library only: it runs with the server's own python3, outside Docker.
"""
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HEALTH = "http://127.0.0.1:8000/health"
DEEP = "http://127.0.0.1:8000/health/deep"  # a test job through the message queue (app/jobs.py canary)
SOAK = 75  # seconds the new version must stay healthy: a worker that can't read the queue shows after 60
WEB = REPO / "web"
# Sidekick Web: web/ is built into web/dist, which Caddy serves at /app. In a throwaway Node container, so the
# server needs no Node; as uid 1000 (ubuntu here, "node" in the image), so the files stay the deploy user's.
WEB_BUILD = ("docker", "run", "--rm", "--user", "1000:1000", "-v", f"{WEB}:/web", "-w", "/web",
             "node:20.18.1-alpine", "sh", "-c", "npm ci --no-audit --no-fund && npm run build")


class Shell:
    """Runs commands in the repo. Tests swap in a fake."""

    def run(self, *cmd: str) -> str:
        return subprocess.run(cmd, cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()

    def succeeds(self, *cmd: str) -> bool:
        return subprocess.run(cmd, cwd=REPO, capture_output=True).returncode == 0


def status_of(url: str, timeout: float = 3) -> int | None:
    """The HTTP status of a GET, or None if nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:  # e.g. 503 from an unhealthy Sidekick
        return exc.code
    except OSError:
        return None


def healthy(wait: float = 90, soak: float = SOAK, sleep=time.sleep, status=status_of, clock=time.monotonic) -> bool:
    """The new version passes when it 1) answers /health within `wait` (it restarts during the deploy), 2) gets
    a test job through the message queue, and 3) stays healthy for `soak` seconds. Answering once isn't enough:
    the workers might fail a few seconds later."""
    deadline = clock() + wait
    while status(HEALTH) != 200:
        if clock() >= deadline:
            print("Health check: /health didn't answer 200 in time")
            return False
        sleep(5)
    if status(DEEP, timeout=30) != 200:
        print("Health check: a test job didn't get through the message queue")
        return False
    end = clock() + soak
    while clock() < end:
        sleep(5)
        if (code := status(HEALTH)) != 200:
            print(f"Health check: /health answered {code} during the {soak:.0f}s watch")
            return False
    return True


def deploy(sh: Shell, healthy=healthy, failed_file: Path = REPO / ".deploy-failed",
           web_built: Path = WEB / "dist" / "index.html") -> str:
    sh.run("git", "fetch", "-q", "origin", "production")
    current, target = sh.run("git", "rev-parse", "HEAD"), sh.run("git", "rev-parse", "origin/production")
    if current == target:
        return "up to date"
    if sh.succeeds("git", "merge-base", "--is-ancestor", target, "HEAD"):
        return "already newer"  # someone deployed a later commit by hand
    if failed_file.exists() and failed_file.read_text().strip() == target:
        return "skipped: this version failed before"  # don't retry a broken version every 5 minutes
    subject = sh.run("git", "log", "-1", "--format=%s", target)
    # The web app is rebuilt only when web/ changed (or was never built): it takes a minute on this small server.
    build_web = not web_built.exists() or not sh.succeeds("git", "diff", "--quiet", current, target, "--", "web")

    sh.run("git", "merge", "--ff-only", "-q", target)
    failure = "failed its health check"
    try:
        if build_web:
            sh.run(*WEB_BUILD)
        sh.run("docker", "compose", "up", "-d", "--build")
        ok = healthy()
    except subprocess.CalledProcessError as exc:  # e.g. the image didn't build: the old container is still running
        ok = False
        failure = f"failed: {' '.join(exc.cmd[:4])} exited with {exc.returncode}"
        last = (exc.stderr or "").strip().splitlines()[-1:]  # the error itself, e.g. "...: read-only file system"
        failure += f" ({last[0][-200:]})" if last else ""
        print(f"{' '.join(exc.cmd)} failed:\n{(exc.stderr or '').strip()[-2000:]}")  # kept in the journal
    if ok:
        failed_file.unlink(missing_ok=True)
        notify(sh, f"🚀 Deployed {target[:7]}: {subject}")
        return f"deployed {target[:7]}"

    sh.run("git", "reset", "-q", "--hard", current)  # back to the version that worked
    try:
        if build_web:
            sh.run(*WEB_BUILD)  # ...and its web app
        sh.run("docker", "compose", "up", "-d", "--build")
    except subprocess.CalledProcessError:
        pass
    failed_file.write_text(target)
    notify(sh, f"⚠️ Deploy of {target[:7]} ({subject}) {failure}. Rolled back to {current[:7]}. "
               "Details: journalctl -u sidekick-deploy")
    return f"rolled back {target[:7]}"


def notify(sh: Shell, text: str) -> None:
    """Tell the owner on Telegram, through the running container (it has the bot token; this script doesn't)."""
    sh.succeeds("docker", "compose", "exec", "-T", "sidekick", "python", "-m", "app.notify", text)


if __name__ == "__main__":
    print(deploy(Shell()))  # systemd keeps this in the journal: journalctl -u sidekick-deploy
    sys.exit(0)
