"""deploy/deploy.py: the server's pull-based deploy. A fake shell records commands; no git, Docker or server."""
import subprocess

import pytest

from deploy.deploy import DEEP, HEALTH, REPO, SOAK, WEB_BUILD, deploy, healthy

OLD, NEW = "a" * 40, "b" * 40


class FakeShell:
    def __init__(self, current=OLD, target=NEW, newer=False, build_fails=False, web_changed=False,
                 web_build_fails=False):
        self.current, self.target, self.newer, self.build_fails = current, target, newer, build_fails
        self.web_changed, self.web_build_fails = web_changed, web_build_fails
        self.commands = []

    def run(self, *cmd):
        self.commands.append(cmd)
        if cmd[:3] == ("git", "rev-parse", "HEAD"):
            return self.current
        if cmd[:3] == ("git", "rev-parse", "origin/production"):
            return self.target
        if cmd[:2] == ("git", "log"):
            return "Read PDF attachments"
        if cmd[:3] == ("docker", "compose", "up") and self.build_fails and len(self.ran("docker", "compose", "up")) == 1:
            raise subprocess.CalledProcessError(1, cmd, stderr="Container sidekick-caddy-1 Creating\nError response "
                                                "from daemon: ... mkdirat .../srv/web: read-only file system")
        if cmd == WEB_BUILD and self.web_build_fails and len(self.ran(*WEB_BUILD)) == 1:  # the new version's
            raise subprocess.CalledProcessError(1, cmd)
        return ""

    def succeeds(self, *cmd):
        self.commands.append(cmd)
        if cmd[:3] == ("git", "diff", "--quiet"):  # "nothing changed in web/" succeeds
            return not self.web_changed
        return self.newer if cmd[:3] == ("git", "merge-base", "--is-ancestor") else True

    def ran(self, *prefix):
        return [c for c in self.commands if c[:len(prefix)] == prefix]

    def notified(self):
        return [c[-1] for c in self.ran("docker", "compose", "exec")]


@pytest.fixture
def failed_file(tmp_path):
    return tmp_path / ".deploy-failed"


@pytest.fixture
def built(tmp_path):
    """The web app was built before (web/dist/index.html exists)."""
    index = tmp_path / "dist" / "index.html"
    index.parent.mkdir()
    index.write_text("<!doctype html>")
    return index


def test_nothing_to_do_when_production_has_not_moved(failed_file):
    sh = FakeShell(current=NEW, target=NEW)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file) == "up to date"
    assert not sh.ran("docker", "compose", "up")


def test_a_new_tested_commit_is_deployed_and_the_owner_hears_about_it(failed_file):
    sh = FakeShell()
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file) == f"deployed {NEW[:7]}"
    assert sh.ran("git", "merge", "--ff-only", "-q", NEW) and sh.ran("docker", "compose", "up", "-d", "--build")
    assert sh.notified() == [f"🚀 Deployed {NEW[:7]}: Read PDF attachments"]


def test_an_unhealthy_deploy_rolls_back_and_is_not_retried_every_5_minutes(failed_file):
    sh = FakeShell()
    assert deploy(sh, healthy=lambda: False, failed_file=failed_file) == f"rolled back {NEW[:7]}"
    assert sh.ran("git", "reset", "-q", "--hard", OLD)                 # code back to the working version...
    assert len(sh.ran("docker", "compose", "up", "-d", "--build")) == 2  # ...and the old container rebuilt
    assert "failed its health check. Rolled back" in sh.notified()[0] and failed_file.read_text() == NEW

    again = FakeShell()
    assert deploy(again, healthy=lambda: True, failed_file=failed_file) == "skipped: this version failed before"
    assert not again.ran("git", "merge")


def test_a_build_failure_also_rolls_back_and_says_what_failed(failed_file, capsys):
    """Regression (Oct 26): compose couldn't start Caddy, and the notice blamed the health check."""
    sh = FakeShell(build_fails=True)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file) == f"rolled back {NEW[:7]}"
    assert sh.ran("git", "reset", "-q", "--hard", OLD)
    notice = sh.notified()[-1]
    assert "docker compose up -d exited with 1" in notice and "read-only file system" in notice
    assert "health check" not in notice and "journalctl -u sidekick-deploy" in notice
    assert "read-only file system" in capsys.readouterr().out  # the whole error is kept in the server's journal


def test_a_newer_hand_deployed_version_is_left_alone(failed_file):
    sh = FakeShell(newer=True)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file) == "already newer"
    assert not sh.ran("git", "merge")


def test_the_next_good_commit_clears_the_failure_mark(failed_file):
    failed_file.write_text(NEW)
    sh = FakeShell(target="c" * 40)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file).startswith("deployed")
    assert not failed_file.exists()


# --- Sidekick Web: web/ is built into web/dist (served by Caddy at /app) -----------------------------------------

def test_the_web_app_is_built_before_the_restart_when_web_changed(failed_file, built):
    sh = FakeShell(web_changed=True)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file, web_built=built) == f"deployed {NEW[:7]}"
    order = [c for c in sh.commands if c == WEB_BUILD or c[:3] == ("docker", "compose", "up")]
    assert order == [WEB_BUILD, ("docker", "compose", "up", "-d", "--build")]  # Caddy restarts on the new files


def test_no_web_build_when_web_didnt_change(failed_file, built):
    sh = FakeShell(web_changed=False)
    deploy(sh, healthy=lambda: True, failed_file=failed_file, web_built=built)
    assert not sh.ran(*WEB_BUILD)


def test_the_first_deploy_builds_the_web_app_even_if_web_didnt_change(failed_file, tmp_path):
    sh = FakeShell(web_changed=False)
    deploy(sh, healthy=lambda: True, failed_file=failed_file, web_built=tmp_path / "dist" / "index.html")
    assert sh.ran(*WEB_BUILD)


def test_a_web_build_that_fails_rolls_back_and_rebuilds_the_working_version(failed_file, built):
    sh = FakeShell(web_changed=True, web_build_fails=True)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file, web_built=built) == f"rolled back {NEW[:7]}"
    assert sh.ran("git", "reset", "-q", "--hard", OLD) and len(sh.ran(*WEB_BUILD)) == 2  # new failed, old rebuilt
    assert failed_file.read_text() == NEW and "failed" in sh.notified()[-1]


# --- The health check that decides between "deployed" and "rolled back" ------------------------------------------

class Server:
    """A fake server on a fake clock: answers(path, second) gives the HTTP status at that moment."""
    def __init__(self, answers):
        self.answers, self.now, self.asked = answers, 0.0, []

    def status(self, url, timeout=3):
        self.asked.append(url)
        return self.answers("deep" if url == DEEP else "health", self.now)

    def check(self):
        return healthy(sleep=self.wait, status=self.status, clock=lambda: self.now)

    def wait(self, seconds):
        self.now += seconds


def test_a_healthy_version_passes_the_deep_check_and_stays_healthy_for_the_whole_watch():
    server = Server(lambda path, t: 200 if t >= 10 else None)  # up after 10 s (the restart)
    assert server.check() is True
    assert server.asked.count(DEEP) == 1 and server.now >= 10 + SOAK


def test_workers_that_cant_process_messages_fail_the_deploy_though_health_says_200():
    server = Server(lambda path, t: 503 if path == "deep" else 200)
    assert server.check() is False


def test_workers_that_stall_a_minute_after_the_restart_fail_the_deploy():
    """Regression for the W4 review: the old check stopped at the first 200, so this deploy would have stayed."""
    server = Server(lambda path, t: 503 if path == "health" and t >= 60 else 200)
    assert server.check() is False


def test_a_version_that_never_answers_fails_after_the_wait():
    server = Server(lambda path, t: None)
    assert server.check() is False and HEALTH in server.asked and DEEP not in server.asked


def test_the_home_page_is_the_landing_page_and_googles_required_pages_stay():
    """Google's sign-in needs a home page and a privacy policy on the domain; sign-in and the app keep their routes."""
    caddy = (REPO / "caddy" / "Caddyfile").read_text()
    home = caddy.split("handle / {", 1)[1].split("}", 1)[0]
    assert "root * /web/dist" in home and "rewrite * /index.html" in home
    for route in ("handle /privacy {", "handle /auth/* {", "handle /oauth/google/callback {", "handle /api/* {",
                  "handle_path /app/* {", "handle /telegram/webhook {", "handle /link {", "handle /join {"):
        assert route in caddy, route
    landing = (REPO / "web" / "src" / "pages" / "Landing.tsx").read_text()
    layout = (REPO / "web" / "src" / "layouts" / "PublicLayout.tsx").read_text()
    assert "PRIVACY_URL" in landing and "https://github.com/np2400/sidekick" in layout  # as the old home page had


def test_the_deep_check_is_only_reachable_from_the_server_itself():
    caddy = (REPO / "caddy" / "Caddyfile").read_text()
    assert "handle /health {" in caddy and "/health*" not in caddy and "/health/" not in caddy
