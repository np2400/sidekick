"""Gmail API, READ-ONLY: search and read the asking user's mail. No send, no delete (scope: gmail.readonly).

The owner uses the server-level sign-in; members use the Gmail they connected with /connect gmail. Which one
is decided by `ctx`, which the server fills in (the model can't pass it)."""
import base64
import html
import io
import logging
import re

from pypdf import PdfReader

from app.google_auth import google_request

API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
MAX_BODY = 1500  # characters (~400 tokens); keeps requests under per-minute token limits (Groq: 8K/min)
MAX_RESULTS = 8     # emails per search: the list is resent on every later call of the answer (was 15; Oct 27,
MAX_SNIPPET = 150   # a member's booking questions hit the per-minute limit after one email, every time)
# Bookings and itineraries often live in PDF attachments. Read them, within limits: a PDF from a stranger is
# untrusted input, and its text shares the same per-minute token budget.
MAX_ATTACHMENT_TEXT = 2500   # characters across all attachments of one email
MAX_PDF_BYTES = 5_000_000
MAX_PDF_PAGES = 6
log = logging.getLogger("sidekick.gmail")
INVISIBLE = re.compile(r"[\u200b-\u200f\u034f\u00ad\ufeff]")  # newsletter padding: costs tokens, means nothing


def _headers(message: dict) -> dict:
    return {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}


def search_emails(query: str = "is:unread newer_than:1d", max_results: int = MAX_RESULTS, *, ctx=None) -> dict:
    """query uses Gmail's own search syntax, exactly like the Gmail search box."""
    data = google_request("GET", API, "Gmail", ctx, params={"q": query, "maxResults": min(max_results, MAX_RESULTS)})
    if "error" in data:
        return data

    emails = []
    for ref in data.get("messages", []):
        # One small request per message: headers + Gmail's short preview, not the full body.
        msg = google_request("GET", f"{API}/{ref['id']}", "Gmail", ctx, params=[
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
            "snippet": INVISIBLE.sub("", html.unescape(msg.get("snippet", ""))).strip()[:MAX_SNIPPET],
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


def _attachment_parts(part: dict) -> list[dict]:
    """Every part with a filename (attachments), anywhere in the MIME tree."""
    found = [part] if part.get("filename") else []
    for child in part.get("parts", []):
        found += _attachment_parts(child)
    return found


def pdf_text(data: bytes) -> str:
    """The text of a PDF's first pages (also for PDFs uploaded on the web). Raises on broken or locked PDFs."""
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def _attachments(message_id: str, payload: dict, ctx) -> list[dict]:
    """Filename for every attachment; text too for PDFs within the limits."""
    out, budget = [], MAX_ATTACHMENT_TEXT
    for part in _attachment_parts(payload):
        item = {"filename": part["filename"]}
        body = part.get("body", {})
        # Some senders label a PDF as a generic file (application/octet-stream): the name tells, the bytes confirm.
        if part.get("mimeType") != "application/pdf" and not part["filename"].lower().endswith(".pdf"):
            item["note"] = "not read (only PDFs are)"
        elif body.get("size", 0) > MAX_PDF_BYTES:
            item["note"] = "not read (too large)"
        elif budget <= 0:
            item["note"] = "not read (text limit for this email reached)"
        else:
            data = body.get("data")
            if not data and body.get("attachmentId"):  # bigger attachments are fetched separately
                fetched = google_request("GET", f"{API}/{message_id}/attachments/{body['attachmentId']}", "Gmail", ctx)
                data = fetched.get("data")
            raw = base64.urlsafe_b64decode(data or "")
            try:
                text = pdf_text(raw) if raw.startswith(b"%PDF") else None
            except Exception as exc:  # broken, encrypted or odd PDFs: say so, never crash the agent
                log.warning("Couldn't read a PDF attachment: %s", type(exc).__name__)
                item["note"] = "couldn't read this PDF (damaged or password-protected)"
            else:
                if text is None:
                    item["note"] = "not read (named .pdf but isn't a PDF)"
                elif not text:  # pages that are pictures (a scan, a ticket image): no text to read
                    item["note"] = ("no text in this PDF (its pages are images, e.g. a scan), so it can't be read; "
                                    "look for the details in the email text or another email")
                else:
                    item["text"], budget = text[:budget], budget - len(text)
                    if len(text) > len(item["text"]):
                        item["truncated"] = True
            # Metadata only (never the name or text): enough to tell why a PDF went unread.
            log.info("PDF attachment: %s, type %s, %d bytes", "read" if "text" in item else item["note"][:40],
                     part.get("mimeType"), len(raw))
        out.append(item)
    return out


def read_email(message_id: str, *, ctx=None) -> dict:
    """The email's text, plus its attachments (PDF text included: itineraries and vouchers often live there)."""
    msg = google_request("GET", f"{API}/{message_id}", "Gmail", ctx, params={"format": "full"})
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
        "attachments": _attachments(message_id, payload, ctx),
    }
