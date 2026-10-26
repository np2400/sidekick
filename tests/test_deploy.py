"""deploy/deploy.py: the server's pull-based deploy. A fake shell records commands; no git, Docker or server."""
import subprocess

import pytest

from deploy.deploy import deploy

OLD, NEW = "a" * 40, "b" * 40


class FakeShell:
    def __init__(self, current=OLD, target=NEW, newer=False, build_fails=False):
        self.current, self.target, self.newer, self.build_fails = current, target, newer, build_fails
        self.commands = []

    def run(self, *cmd):
        self.commands.append(cmd)
        if cmd[:3] == ("git", "rev-parse", "HEAD"):
            return self.current
        if cmd[:3] == ("git", "rev-parse", "origin/production"):
            return self.target
        if cmd[:2] == ("git", "log"):
            return "Read PDF attachments"
        if cmd[:3] == ("docker", "compose", "up") and self.build_fails:
            raise subprocess.CalledProcessError(1, cmd)
        return ""

    def succeeds(self, *cmd):
        self.commands.append(cmd)
        return self.newer if cmd[:3] == ("git", "merge-base", "--is-ancestor") else True

    def ran(self, *prefix):
        return [c for c in self.commands if c[:len(prefix)] == prefix]

    def notified(self):
        return [c[-1] for c in self.ran("docker", "compose", "exec")]


@pytest.fixture
def failed_file(tmp_path):
    return tmp_path / ".deploy-failed"


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
    assert "Rolled back" in sh.notified()[0] and failed_file.read_text() == NEW

    again = FakeShell()
    assert deploy(again, healthy=lambda: True, failed_file=failed_file) == "skipped: this version failed before"
    assert not again.ran("git", "merge")


def test_a_build_failure_also_rolls_back(failed_file):
    sh = FakeShell(build_fails=True)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file) == f"rolled back {NEW[:7]}"
    assert sh.ran("git", "reset", "-q", "--hard", OLD)


def test_a_newer_hand_deployed_version_is_left_alone(failed_file):
    sh = FakeShell(newer=True)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file) == "already newer"
    assert not sh.ran("git", "merge")


def test_the_next_good_commit_clears_the_failure_mark(failed_file):
    failed_file.write_text(NEW)
    sh = FakeShell(target="c" * 40)
    assert deploy(sh, healthy=lambda: True, failed_file=failed_file).startswith("deployed")
    assert not failed_file.exists()
