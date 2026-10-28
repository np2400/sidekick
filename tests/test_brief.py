"""The personal morning brief: code sorts email (people vs automated), groups and filters it by each person's
preferences, and the model only chooses and writes. Oct 28: one mixed list made automated job pitches and newsletters
look like personal emails needing a reply. Offline: Gmail and the model are fakes."""
import json

import pytest

from app import brief, daily, db, gmail, google_web, settings
from app.trace import ToolContext
from tests.fakes import ScriptedProvider, text

OWNER, DAD = 111, 222


def headers(**h) -> dict:
    return {k.replace("_", "-"): v for k, v in h.items()}


# --- People vs automated, decided in code ----------------------------------------------------------------------

@pytest.mark.parametrize("from_, extra, labels, expected", [
    ("Maya Lee <maya@gmail.com>", {}, ["INBOX", "CATEGORY_PERSONAL"], ("person", "")),
    ("Kevin Jobson <kevin.jobson@gmail.com>", {}, [], ("person", "")),            # "jobs" inside a name
    ("LinkedIn <jobalerts-noreply@linkedin.com>", {}, ["CATEGORY_UPDATES"], ("automated", "updates")),
    ("Aj Mosot <seekerteam@ziprecruiter.com>", {"List_Unsubscribe": "<mailto:u@z.com>"}, [], ("automated", "updates")),
    ("UNIQLO <email@us.edm.uniqlo.com>", {}, ["CATEGORY_PROMOTIONS"], ("automated", "promotions")),
    ("Friends <group@lists.example>", {"List_Id": "<friends.lists.example>"}, ["CATEGORY_FORUMS"], ("automated", "forums")),
    ("Server <ops@acme.com>", {"Auto_Submitted": "auto-generated"}, [], ("automated", "updates")),
    ("Bank <alerts@bank.example>", {}, [], ("automated", "updates")),              # the address says it
])
def test_who_an_email_is_from_is_decided_by_gmail_and_headers_not_the_model(from_, extra, labels, expected):
    assert gmail.email_kind(headers(From=from_, **extra), labels) == expected


def test_the_digest_sorts_unread_mail_names_people_and_counts_everything(monkeypatch):
    inbox = {
        "p1": {"labelIds": ["CATEGORY_PERSONAL"], "headers": {"From": "Maya Lee <maya@gmail.com>",
                                                              "Subject": "Dinner Friday?"}},
        "a1": {"labelIds": ["CATEGORY_UPDATES"], "headers": {"From": "LinkedIn <jobalerts-noreply@linkedin.com>",
                                                             "Subject": "FDE at Deloitte"}},
    }

    def fake_request(method, url, service, ctx=None, params=None, **kwargs):
        if url == gmail.API:
            return {"messages": [{"id": i} for i in inbox], "resultSizeEstimate": 41}
        mid = url.rsplit("/", 1)[1]
        return {"id": mid, "snippet": "…", "labelIds": inbox[mid]["labelIds"],
                "payload": {"headers": [{"name": k, "value": v} for k, v in inbox[mid]["headers"].items()]}}
    monkeypatch.setattr(gmail, "google_request", fake_request)

    digest = gmail.unread_digest()
    assert digest["total"] == 41  # Gmail's count of all of it, not just the 30 looked at
    assert [(e["from"], e["subject"]) for e in digest["people"]] == [("Maya Lee", "Dinner Friday?")]  # no address
    assert [(e["from"], e["category"]) for e in digest["automated"]] == [("LinkedIn", "updates")]


# --- Grouped, filtered by the person's choices --------------------------------------------------------------------

def automated(sender, category, subject):
    return {"from": sender, "category": category, "subject": subject, "snippet": f"{subject}…", "date": "today"}


def test_automated_mail_is_grouped_by_sender_and_left_out_categories_are_only_counted():
    digest = {"total": 9, "people": [{"from": "Maya", "subject": "Dinner Friday?"}], "automated": [
        automated("LinkedIn Job Alerts", "updates", "FDE at Deloitte"),
        automated("LinkedIn Job Alerts", "updates", "FDE at Quadric"),
        automated("LinkedIn Job Alerts", "updates", "FDE at Quadric"),   # the same alert twice
        automated("UNIQLO", "promotions", "Sale from $7.90"),
        automated("Chipotle", "promotions", "Boorito returns"),
        automated("Nextdoor", "social", "Irvine Lake draining")]}
    facts = brief.email_facts(digest, hidden=["promotions"])

    assert facts["from_people"] == [{"from": "Maya", "subject": "Dinner Friday?"}]
    linkedin, nextdoor = facts["automated_groups"]
    assert linkedin["count"] == 3 and linkedin["subjects"] == ["FDE at Deloitte", "FDE at Quadric"]  # one item
    assert nextdoor["from"] == "Nextdoor"
    assert facts["left_out_by_choice"] == {"promotions": 2} and facts["total_unread"] == 9

    everything = brief.email_facts(digest, hidden=[])
    assert {g["from"] for g in everything["automated_groups"]} == {"LinkedIn Job Alerts", "UNIQLO", "Chipotle",
                                                                    "Nextdoor"}


# --- Preferences: each person's own, sensible without any ----------------------------------------------------------

def test_preferences_are_per_person_encrypted_and_default_to_leaving_out_deals():
    assert settings.brief_preferences(DAD) == {"interests": "", "hidden": ["promotions"]}  # never set: defaults
    settings.set_brief_preferences(OWNER, "  Job hunting:\n Forward Deployed Engineer roles  ", ["social"])
    settings.set_brief_preferences(DAD, "Family visits and travel to India", [])

    assert settings.brief_preferences(OWNER) == {"interests": "Job hunting: Forward Deployed Engineer roles",
                                                 "hidden": ["social"]}
    assert settings.brief_preferences(DAD)["interests"] == "Family visits and travel to India"
    with db.connect() as conn:
        stored = conn.execute("SELECT interests_encrypted FROM brief_preferences WHERE account_id = ?", (DAD,)).fetchone()
    assert "India" not in stored[0]  # their words are encrypted at rest

    with pytest.raises(ValueError):
        settings.set_brief_preferences(DAD, "x" * (settings.MAX_INTERESTS + 1), [])
    with pytest.raises(ValueError):
        settings.set_brief_preferences(DAD, "travel", ["spam"])


def test_the_brief_gets_this_persons_priorities_and_its_own_short_instructions(monkeypatch):
    settings.set_brief_preferences(OWNER, "Travel and family", [])
    monkeypatch.setattr(brief, "list_events", lambda start, end, ctx: {"count": 0, "events": []})
    monkeypatch.setattr(brief, "unread_digest", lambda ctx: {"total": 3, "people": [], "automated": [
        automated("United", "updates", "Your flight to Mumbai has a new gate")]})
    llm = ScriptedProvider([text("Today: Nothing on your calendar.\nFor you: United changed your gate.")])

    out = brief.build_brief(ctx=ToolContext(chat_id=OWNER), llm=llm)
    assert out.startswith("☀️ ") and out.count("☀️") == 1 and out.endswith("United changed your gate.")
    prompt = llm.received[0][0].text
    facts = json.loads(prompt[len(brief.BRIEF_PROMPT):])
    assert facts["what_matters_to_user"] == "Travel and family"
    assert facts["email"]["automated_groups"][0]["from"] == "United"
    assert "Nothing from automated_groups ever goes here." in prompt and "at most 2 items" in prompt
    assert llm.sensitive_flags == [True]  # email previews: only models allowed to see private content


def test_the_latest_brief_is_kept_encrypted_for_the_web():
    assert brief.latest(DAD) is None
    brief.save_latest(DAD, "☀️ Thursday Oct 29\n\nToday: Nothing on your calendar.")
    brief.save_latest(DAD, "☀️ Friday Oct 30\n\nToday: Temple at 6 PM.")  # replaces the one before
    assert brief.latest(DAD)["text"].endswith("Temple at 6 PM.") and brief.latest(DAD)["made_at"].endswith("Z")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM briefs WHERE body_encrypted LIKE '%Temple%'").fetchone()[0] == 0


def test_a_members_scheduled_brief_uses_the_google_account_they_connected(monkeypatch):
    """Oct 28: the scheduler built contexts without these flags, so members' briefs had no calendar or email."""
    monkeypatch.setattr(google_web, "connection", lambda account_id: {"calendar": True, "gmail": True})
    dad = daily.context(DAD, "member")
    assert dad.calendar and dad.gmail and dad.role == "member"
    owner = daily.context(OWNER, "owner")  # the owner's Google is the server's own sign-in, not google_web
    assert not owner.calendar and not owner.gmail and owner.role == "owner"


# --- On the web: each person's own brief and settings ------------------------------------------------------------

SITE = "https://sidekick.example.org"


@pytest.fixture
def web(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, users, web_api, web_login
    monkeypatch.setattr(web_login, "SIDEKICK_DOMAIN", "sidekick.example.org")
    monkeypatch.setattr(web_api, "TELEGRAM_OWNER_ID", str(OWNER))
    users.ensure_owner(OWNER)
    users.redeem(users.create_invite(OWNER), DAD, "Dad")
    made = []
    monkeypatch.setattr(brief, "build_brief", lambda ctx: made.append(ctx) or f"☀️ Brief for {ctx.chat_id}")

    def browser(who):
        client = TestClient(main.app, base_url=SITE, headers={"Origin": SITE})
        client.cookies.set(web_login.COOKIE, web_login.new_session(who))
        return client
    return {"browser": browser, "made": made}


def test_each_person_sets_their_own_brief_time_priorities_and_categories(web):
    me, dad = web["browser"](OWNER), web["browser"](DAD)
    assert dad.get("/api/settings").json()["brief_time"] is None  # members: off until they turn it on
    assert dad.get("/api/settings").json()["brief"]["hidden"] == ["promotions"]

    saved = dad.post("/api/settings/brief", json={"time": "7:30am", "interests": "Family visits, travel to India",
                                                  "hidden": ["promotions", "social"]}).json()
    assert saved["brief_time"] == "07:30" and saved["brief"]["interests"] == "Family visits, travel to India"
    assert saved["brief"]["hidden"] == ["social", "promotions"] and "updates" in saved["brief"]["categories"]
    assert me.get("/api/settings").json()["brief"]["interests"] == ""  # Dad's words stay Dad's

    assert dad.post("/api/settings/brief", json={"time": "off", "interests": "", "hidden": []}).json()["brief_time"] is None
    assert dad.post("/api/settings/brief", json={"time": "25:00", "interests": ""}).status_code == 422
    assert dad.post("/api/settings/brief", json={"time": "8:00", "interests": "", "hidden": ["spam"]}).status_code == 422
    assert dad.post("/api/settings/brief", json={"time": "8:00", "interests": "x" * 401}).status_code == 422


def test_make_my_brief_now_shows_only_your_own_and_not_twice_a_minute(web):
    me, dad = web["browser"](OWNER), web["browser"](DAD)
    assert me.get("/api/brief").json() == {"latest": None}

    made = dad.post("/api/brief").json()["latest"]
    assert made["text"] == f"☀️ Brief for {DAD}" and web["made"][0].role == "member"
    assert dad.post("/api/brief").status_code == 429 and len(web["made"]) == 1  # one AI call a minute at most
    assert me.get("/api/brief").json() == {"latest": None}  # never someone else's
    assert dad.get("/api/brief").json()["latest"]["text"] == f"☀️ Brief for {DAD}"

    outsider = web["browser"](OWNER)
    outsider.headers["Origin"] = "https://evil.example"
    assert outsider.post("/api/brief").status_code == 403  # only from Sidekick's own pages
