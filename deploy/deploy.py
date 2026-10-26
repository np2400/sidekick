"""Pull-based continuous deployment, run on the server every 5 minutes by a systemd timer.

CI moves the `production` branch to a commit only after the tests and the secret scan pass on main. This script
notices when `production` moves, deploys that commit, checks /health, and rolls back if Sidekick doesn't come up.
Nothing outside reaches in: the server only pulls (no SSH from GitHub, no server keys stored in GitHub).

Standard library only: it runs with the server's own python3, outside Docker.
"""
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HEALTH = "http://127.0.0.1:8000/health"


class Shell:
    """Runs commands in the repo. Tests swap in a fake."""

    def run(self, *cmd: str) -> str:
        return subprocess.run(cmd, cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()

    def succeeds(self, *cmd: str) -> bool:
        return subprocess.run(cmd, cwd=REPO, capture_output=True).returncode == 0


def healthy(wait: float = 90, sleep=time.sleep) -> bool:
    """Wait for the new container to answer /health (it restarts during the deploy)."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH, timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        sleep(5)
    return False


def deploy(sh: Shell, healthy=healthy, failed_file: Path = REPO / ".deploy-failed") -> str:
    sh.run("git", "fetch", "-q", "origin", "production")
    current, target = sh.run("git", "rev-parse", "HEAD"), sh.run("git", "rev-parse", "origin/production")
    if current == target:
        return "up to date"
    if sh.succeeds("git", "merge-base", "--is-ancestor", target, "HEAD"):
        return "already newer"  # someone deployed a later commit by hand
    if failed_file.exists() and failed_file.read_text().strip() == target:
        return "skipped: this version failed before"  # don't retry a broken version every 5 minutes
    subject = sh.run("git", "log", "-1", "--format=%s", target)

    sh.run("git", "merge", "--ff-only", "-q", target)
    try:
        sh.run("docker", "compose", "up", "-d", "--build")
        ok = healthy()
    except subprocess.CalledProcessError:  # e.g. the image didn't build: the old container is still running
        ok = False
    if ok:
        failed_file.unlink(missing_ok=True)
        notify(sh, f"🚀 Deployed {target[:7]}: {subject}")
        return f"deployed {target[:7]}"

    sh.run("git", "reset", "-q", "--hard", current)  # back to the version that worked
    try:
        sh.run("docker", "compose", "up", "-d", "--build")
    except subprocess.CalledProcessError:
        pass
    failed_file.write_text(target)
    notify(sh, f"⚠️ Deploy of {target[:7]} ({subject}) failed its health check. Rolled back to {current[:7]}.")
    return f"rolled back {target[:7]}"


def notify(sh: Shell, text: str) -> None:
    """Tell the owner on Telegram, through the running container (it has the bot token; this script doesn't)."""
    sh.succeeds("docker", "compose", "exec", "-T", "sidekick", "python", "-m", "app.notify", text)


if __name__ == "__main__":
    print(deploy(Shell()))  # systemd keeps this in the journal: journalctl -u sidekick-deploy
    sys.exit(0)
