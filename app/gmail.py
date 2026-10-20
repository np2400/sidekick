"""Gmail API, READ-ONLY: search and read the owner's mail. No send, no delete (scope: gmail.readonly)."""
import base64
import html
import re

from app.google_auth import google_request

API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
MAX_BODY = 1500  # characters (~400 tokens); keeps requests under per-minute token limits (Groq: 8K/min)
INVISIBLE = re.compile(r"[\u200b-\u200f\u034f\u00ad\ufeff]")  # newsletter padding: costs tokens, means nothing


def _headers(message: dict) -> dict:
    return {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}


def search_emails(query: str = "is:unread newer_than:1d", max_results: int = 10) -> dict:
    """query uses Gmail's own search syntax, exactly like the Gmail search box."""
    data = google_request("GET", API, "Gmail", params={"q": query, "maxResults": min(max_results, 15)})
    if "error" in data:
        return data

    emails = []
    for ref in data.get("messages", []):
        # One small request per message: headers + Gmail's short preview, not the full body.
        msg = google_request("GET", f"{API}/{ref['id']}", "Gmail", params=[
            ("format", "metadata"), ("metadataHeaders", "From"),
            ("metadataHeaders", "Subject"), ("metadataHeaders", "Date"),
        ])
        if "error" in msg:
            return msg
        h = _headers(msg)
        emails.append({
            "id": msg["id"],
            "from": h.get("From", ""),
            "subject": h.get("Subject", "(no subject)"),
            "date": h.get("Date", ""),
            # Newsletters pad previews with invisible characters; drop them (they cost tokens).
            "snippet": INVISIBLE.sub("", html.unescape(msg.get("snippet", ""))).strip(),
        })
    return {"query": query, "count": len(emails), "emails": emails}


def _find_text(part: dict, mime: str) -> str:
    """Emails are a tree of MIME parts; walk it to find the first body of the given type."""
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    for child in part.get("parts", []):
        text = _find_text(child, mime)
        if text:
            return text
    return ""


def read_email(message_id: str) -> dict:
    msg = google_request("GET", f"{API}/{message_id}", "Gmail", params={"format": "full"})
    if "error" in msg:
        return msg
    payload = msg.get("payload", {})
    body = _find_text(payload, "text/plain")
    if not body:  # HTML-only email: crude tag strip is enough for the model to read it
        body = re.sub(r"<[^>]+>", " ", _find_text(payload, "text/html"))
        body = re.sub(r"\s+", " ", html.unescape(body))
    body = re.sub(r"[ \t]+", " ", INVISIBLE.sub("", body)).strip()
    h = _headers(msg)
    return {
        "from": h.get("From", ""),
        "subject": h.get("Subject", ""),
        "date": h.get("Date", ""),
        "body": body[:MAX_BODY],
        "truncated": len(body) > MAX_BODY,
    }
