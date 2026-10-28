"""Gmail API, READ-ONLY: search and read the asking user's mail. No send, no delete (scope: gmail.readonly).

The owner uses the server-level sign-in; members use the Gmail they connected with /connect gmail. Which one
is decided by `ctx`, which the server fills in (the model can't pass it)."""
import base64
import html
import io
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

from pypdf import PdfReader, PdfWriter

from app.config import OWNER_TIMEZONE
from app.weather import when as day_label
from app.google_auth import google_request
from app.llm.types import Image

API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
# Sizes for the paid model first in line (OpenRouter, no per-minute limit). A request too big for Groq's free
# 8K tokens/minute is passed on to the next model by the router. Oct 27: cutting searches to 8 emails with
# 150-character previews (for Groq) lost an airline booking number a member had been given before.
MAX_BODY = 6000     # characters of an email's text (~1,500 tokens): long airline emails keep the details lower down
MAX_RESULTS = 15    # emails per search
MAX_SNIPPET = 300   # characters per preview: Gmail's own are about 200, so this keeps them whole
# Bookings and itineraries often live in PDF attachments. Read them, within limits: a PDF from a stranger is
# untrusted input, and its text shares the same per-minute token budget.
MAX_ATTACHMENT_TEXT = 2500   # characters across all attachments of one email
MAX_PDF_BYTES = 5_000_000
MAX_PDF_PAGES = 6
# Images in an email (attached, or pasted into its body) can hold what the text doesn't: a ticket, a QR page, a
# scanned receipt. read_email lists them and the model looks at one with view_attachment, so an email with a banner
# doesn't send every answer to a photo model. Images linked from the web are never loaded: that tells the sender
# the email was opened (tracking pixels), and they're mostly logos.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}  # what Gemini can see
MIN_IMAGE_BYTES = 10_000     # smaller: logos, icons, spacers
MAX_IMAGE_BYTES = 5_000_000
LOOK = "call view_attachment with its attachment number to look at it"
log = logging.getLogger("sidekick.gmail")
INVISIBLE = re.compile(r"[\u200b-\u200f\u034f\u00ad\ufeff]")  # newsletter padding: costs tokens, means nothing


def clock() -> datetime:
    return datetime.now(timezone.utc)  # replaced in tests, so "today" and "yesterday" don't depend on the real date


def _local_date(header: str, ctx) -> str:
    """An email's Date header in the user's timezone, labelled "today", "yesterday"...: senders often write UTC,
    and models repeat it as given (Oct 27: "Oct 25, 05:35 UTC" for a payment made the evening of Oct 24), or call
    last night's email today's (Oct 28). Unparseable: left as it is."""
    tz = ZoneInfo(getattr(ctx, "tz", None) or OWNER_TIMEZONE)
    try:
        when = parsedate_to_datetime(header).astimezone(tz)
    except (TypeError, ValueError, IndexError):
        return header
    label = day_label(when.date(), clock().astimezone(tz).date())
    return f"{when:%a %b} {when.day} {when:%Y}, {when.hour % 12 or 12}:{when:%M %p} ({when.tzname()}), {label}"


def _headers(message: dict) -> dict:
    return {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}


DIRECTIONS = {"any": "", "sent": "from:me", "received": "-from:me"}  # sent includes emails to themselves


def _part_fields(depth: int) -> str:
    """Gmail fields for a MIME part, nested `depth` levels: the structure and sizes, never the content."""
    part = "mimeType,filename,body(size,attachmentId)"
    return part if depth == 0 else f"{part},parts({_part_fields(depth - 1)})"


# Per search result: headers, Gmail's preview and the MIME tree (to count images and PDFs), without downloading
# any text or file: a search lists up to 15 emails.
SEARCH_FIELDS = f"id,snippet,payload(headers,{_part_fields(4)})"


def day_bounds(day: str, tz: str) -> tuple[int, int]:
    """"today", "yesterday" or YYYY-MM-DD, as that whole day in the user's timezone, in Unix seconds. Gmail's own
    newer_than:1d is the last 24 hours, and its after:/before: dates use its own timezone; seconds are exact."""
    zone = ZoneInfo(tz)
    today = clock().astimezone(zone).date()
    wanted = {"today": today, "yesterday": today - timedelta(days=1)}.get(day.strip().lower())
    wanted = wanted or date.fromisoformat(day.strip())  # ValueError: not a day
    start, end = (datetime.combine(d, time(), zone) for d in (wanted, wanted + timedelta(days=1)))
    return int(start.timestamp()), int(end.timestamp())


def _contents(payload: dict) -> str:
    """What an email carries besides text, e.g. "1 image, 1 PDF": an email that's only a picture shows here."""
    images = pdfs = 0
    for part in _attachment_parts(payload):
        if part.get("mimeType") in IMAGE_TYPES and part.get("body", {}).get("size", 0) >= MIN_IMAGE_BYTES:
            images += 1
        elif _is_pdf(part):
            pdfs += 1
    counts = [f"{n} {kind}{'s' if n > 1 else ''}" for n, kind in ((images, "image"), (pdfs, "PDF")) if n]
    return ", ".join(counts)


# How the brief tells mail from a person apart from automated mail: Gmail's own inbox categories, then the headers
# bulk senders must send, then the sender's address. Code decides, so an automated "we'd love to talk to you" can
# never be presented as a personal email that needs a reply.
GMAIL_CATEGORIES = {"CATEGORY_PROMOTIONS": "promotions", "CATEGORY_SOCIAL": "social", "CATEGORY_UPDATES": "updates",
                    "CATEGORY_FORUMS": "forums"}
AUTOMATED_SENDER = re.compile(r"(?:^|[._+-])(no-?reply|do-?not-?reply|notifications?|alerts?|mailer-daemon|"
                              r"newsletters?|news|updates?|marketing|info|hello|support|jobs?)(?:[._+-][^@]*)?@",
                              re.I)  # whole words only: kevin.jobson@ is a person
DIGEST_HEADERS = ("From", "Subject", "Date", "List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted")


def sender_name(from_header: str) -> str:
    """'Maya Lee <maya@x.com>' -> 'Maya Lee': the brief names people, it doesn't list addresses."""
    name, address = parseaddr(from_header)
    return name.strip().strip('"') or address


def email_kind(headers: dict, labels: list[str]) -> tuple[str, str]:
    """("person", "") or ("automated", category). Category: Gmail's tab, else "updates" for other automated mail."""
    category = next((GMAIL_CATEGORIES[label] for label in labels if label in GMAIL_CATEGORIES), "")
    bulk = (headers.get("List-Unsubscribe") or headers.get("List-Id")
            or headers.get("Precedence", "").lower() in {"bulk", "list", "junk"}
            or headers.get("Auto-Submitted", "no").lower() != "no"
            or AUTOMATED_SENDER.search(parseaddr(headers.get("From", ""))[1] or ""))
    if category or bulk:
        return "automated", category or "updates"
    return "person", ""


def unread_digest(max_emails: int = 30, *, ctx=None) -> dict:
    """The last day's unread mail for the brief, sorted in code: from people, or automated (with its category).
    Headers and Gmail's preview only, never bodies. total: Gmail's estimate of all of it, beyond max_emails."""
    data = google_request("GET", API, "Gmail", ctx,
                          params={"q": "is:unread newer_than:1d -in:spam -in:chats", "maxResults": max_emails})
    if "error" in data:
        return data
    people, automated = [], []
    for ref in data.get("messages", []):
        msg = google_request("GET", f"{API}/{ref['id']}", "Gmail", ctx,
                             params=[("format", "metadata"), *(("metadataHeaders", h) for h in DIGEST_HEADERS)])
        if "error" in msg:
            return msg
        h = _headers(msg)
        kind, category = email_kind(h, msg.get("labelIds", []))
        email = {"from": sender_name(h.get("From", "")), "subject": h.get("Subject") or "(no subject)",
                 "date": _local_date(h.get("Date", ""), ctx),
                 "snippet": INVISIBLE.sub("", html.unescape(msg.get("snippet", ""))).strip()[:MAX_SNIPPET]}
        if kind == "person":
            people.append(email)
        else:
            automated.append({**email, "category": category})
    return {"total": max(data.get("resultSizeEstimate", 0), len(people) + len(automated)),
            "people": people, "automated": automated}


def search_emails(query: str = "", day: str = "", direction: str = "any", max_results: int = MAX_RESULTS, *,
                  ctx=None) -> dict:
    """query uses Gmail's own search syntax. day and direction narrow it in code: the user's own day, and emails
    they sent or received. With neither words nor day, today's unread."""
    if direction not in DIRECTIONS:
        return {"error": "direction must be any, sent or received."}
    terms = [query.strip(), DIRECTIONS[direction]]
    if day:
        try:
            start, end = day_bounds(day, getattr(ctx, "tz", None) or OWNER_TIMEZONE)
        except ValueError:
            return {"error": "day must be today, yesterday or a date like 2026-10-28."}
        terms.append(f"after:{start} before:{end}")
    full_query = " ".join(t for t in terms if t) or "is:unread newer_than:1d"
    data = google_request("GET", API, "Gmail", ctx,
                          params={"q": full_query, "maxResults": min(max_results, MAX_RESULTS)})
    if "error" in data:
        return data
    note = ""
    narrowing = " ".join(t for t in terms[1:] if t)  # the day and direction, without the words
    if query.strip() and narrowing and not data.get("messages"):
        # The words found nothing, but day or direction narrow it: list those emails without the words (at most 15).
        # Gmail can't search words inside an image (Oct 28: "the receipt I emailed myself today" was a photo).
        full_query = narrowing
        data = google_request("GET", API, "Gmail", ctx,
                              params={"q": full_query, "maxResults": min(max_results, MAX_RESULTS)})
        if "error" in data:
            return data
        note = (f"No email had the words '{query.strip()}', so these are all the emails for the day and direction "
                "you asked (words inside images can't be searched): check their attachments.")

    emails = []
    for ref in data.get("messages", []):
        msg = google_request("GET", f"{API}/{ref['id']}", "Gmail", ctx,
                             params={"format": "full", "fields": SEARCH_FIELDS})
        if "error" in msg:  # e.g. Gmail refused the fields list: headers and preview only, as before (no counts)
            log.warning("Search with attachment counts failed; falling back to headers only")
            msg = google_request("GET", f"{API}/{ref['id']}", "Gmail", ctx, params=[
                ("format", "metadata"), ("metadataHeaders", "From"),
                ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])
        if "error" in msg:
            return msg
        h = _headers(msg)
        email = {
            "id": msg["id"],
            "from": h.get("From", ""),
            "subject": h.get("Subject") or "(no subject)",
            "date": _local_date(h.get("Date", ""), ctx),
            # Newsletters pad previews with invisible characters; drop them (they cost tokens).
            "snippet": INVISIBLE.sub("", html.unescape(msg.get("snippet", ""))).strip()[:MAX_SNIPPET],
        }
        if carries := _contents(msg.get("payload", {})):
            email["attachments"] = carries
        emails.append(email)
    return {"query": full_query, "count": len(emails), "emails": emails, **({"note": note} if note else {})}


def _find_text(part: dict, mime: str) -> str:
    """Emails are a tree of MIME parts; walk it to find the first body of the given type."""
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    for child in part.get("parts", []):
        text = _find_text(child, mime)
        if text:
            return text
    return ""


def html_text(page: str) -> str:
    """An HTML email's readable text: without its <style>/<script> code (newsletters start with thousands of
    characters of it), with a line break where a paragraph, row or line ends."""
    page = re.sub(r"(?is)<(head|style|script)\b.*?</\1\s*>|<!--.*?-->", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table)\s*>", "\n", page)
    page = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return re.sub(r"\s*\n\s*", "\n", re.sub(r"[ \t\r\f\v\xa0]+", " ", page)).strip()


def _attachment_parts(part: dict) -> list[dict]:
    """Every attachment anywhere in the MIME tree: parts with a filename, and images pasted into the body (those
    often have none). Their order is the attachment number read_email and view_attachment share."""
    pasted = part.get("mimeType", "").startswith("image/") and part.get("body", {}).get("attachmentId")
    found = [part] if part.get("filename") or pasted else []
    for child in part.get("parts", []):
        found += _attachment_parts(child)
    return found


def _name(part: dict, number: int) -> str:
    return part.get("filename") or f"image {number} in the email"


def _is_pdf(part: dict) -> bool:
    # Some senders label a PDF as a generic file (application/octet-stream): the name tells, the bytes confirm.
    return part.get("mimeType") == "application/pdf" or part.get("filename", "").lower().endswith(".pdf")


def _download(message_id: str, part: dict, ctx) -> bytes:
    body = part.get("body", {})
    data = body.get("data")
    if not data and body.get("attachmentId"):  # bigger attachments are fetched separately
        data = google_request("GET", f"{API}/{message_id}/attachments/{body['attachmentId']}", "Gmail", ctx).get("data")
    return base64.urlsafe_b64decode(data or "")


def pdf_text(data: bytes) -> str:
    """The text of a PDF's first pages (also for PDFs uploaded on the web). Raises on broken or locked PDFs."""
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def _attachments(message_id: str, payload: dict, ctx) -> list[dict]:
    """Every attachment, numbered: text for PDFs within the limits, and images to look at with view_attachment."""
    out, budget = [], MAX_ATTACHMENT_TEXT
    for number, part in enumerate(_attachment_parts(payload), start=1):
        item = {"attachment": number, "filename": _name(part, number)}
        mime, size = part.get("mimeType", ""), part.get("body", {}).get("size", 0)
        if mime in IMAGE_TYPES:
            item["note"] = ("not shown (a small image, like a logo)" if size < MIN_IMAGE_BYTES else
                            "not shown (too large)" if size > MAX_IMAGE_BYTES else f"an image: {LOOK}")
        elif not _is_pdf(part):
            item["note"] = "not read (only PDFs and images are)"
        elif size > MAX_PDF_BYTES:
            item["note"] = "not read (too large)"
        elif budget <= 0:
            item["note"] = "not read (text limit for this email reached)"
        else:
            raw = _download(message_id, part, ctx)
            try:
                text = pdf_text(raw) if raw.startswith(b"%PDF") else None
            except Exception as exc:  # broken, encrypted or odd PDFs: say so, never crash the agent
                log.warning("Couldn't read a PDF attachment: %s", type(exc).__name__)
                item["note"] = "couldn't read this PDF (damaged or password-protected)"
            else:
                if text is None:
                    item["note"] = "not read (named .pdf but isn't a PDF)"
                elif not text:  # pages that are pictures (a scan, a ticket image): no text, but a model can look
                    item["note"] = f"no text in this PDF (its pages are images, e.g. a scan): {LOOK}"
                else:
                    item["text"], budget = text[:budget], budget - len(text)
                    if len(text) > len(item["text"]):
                        item["truncated"] = True
            # Metadata only (never the name or text): enough to tell why a PDF went unread.
            log.info("PDF attachment: %s, type %s, %d bytes", "read" if "text" in item else item["note"][:40],
                     mime, len(raw))
        out.append(item)
    return out


def first_pages(data: bytes) -> bytes:
    """At most MAX_PDF_PAGES pages: a model reads, and is paid for, every page it gets."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) <= MAX_PDF_PAGES:
            return data
        writer = PdfWriter()
        for page in reader.pages[:MAX_PDF_PAGES]:
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:  # an odd PDF the reader can't split: sent whole (it's at most MAX_PDF_BYTES)
        return data


def view_attachment(message_id: str, attachment: int, *, ctx=None) -> dict:
    """One image, or a PDF whose pages are images, from an email (numbered as read_email lists them), for the model
    to look at. It's returned under "_images": the agent loop passes it on as a picture, never as text."""
    msg = google_request("GET", f"{API}/{message_id}", "Gmail", ctx, params={"format": "full"})
    if "error" in msg:
        return msg
    parts = _attachment_parts(msg.get("payload", {}))
    if not 1 <= attachment <= len(parts):
        return {"error": f"That email has no attachment {attachment} (it has {len(parts)})."}
    part = parts[attachment - 1]
    name, mime, size = _name(part, attachment), part.get("mimeType", ""), part.get("body", {}).get("size", 0)
    if mime in IMAGE_TYPES:
        if not MIN_IMAGE_BYTES <= size <= MAX_IMAGE_BYTES:
            return {"error": f"{name} can't be shown: it's logo-sized or too large."}
        data = _download(message_id, part, ctx)
    elif _is_pdf(part):
        if size > MAX_PDF_BYTES:
            return {"error": f"{name} can't be shown: it's too large."}
        data, mime = first_pages(_download(message_id, part, ctx)), "application/pdf"
        if not data.startswith(b"%PDF"):
            return {"error": f"{name} isn't really a PDF."}
    else:
        return {"error": f"{name} isn't an image or a PDF, so it can't be shown."}
    if not data:
        return {"error": f"Couldn't download {name}."}
    log.info("Showing an email attachment: type %s, %d bytes", mime, len(data))  # metadata only
    return {"summary": f"{name} follows as an image for you to look at.", "_images": [Image(mime=mime, data=data, from_email=True)]}


def read_email(message_id: str, *, ctx=None) -> dict:
    """The email's text, plus its attachments (PDF text included: itineraries and vouchers often live there)."""
    msg = google_request("GET", f"{API}/{message_id}", "Gmail", ctx, params={"format": "full"})
    if "error" in msg:
        return msg
    payload = msg.get("payload", {})
    # Most emails carry two copies. The plain one is sometimes just "View your order: <link>" while the HTML one
    # (what a mail app shows) has the order table (Oct 28, a pizza order's price): read the HTML when it says more.
    plain, rich = _find_text(payload, "text/plain"), html_text(_find_text(payload, "text/html"))
    words = len(re.sub(r"https?://\S+", "", plain))  # a long tracking link doesn't make the plain copy say more
    body = rich if len(rich) > 1.5 * words else plain
    body = re.sub(r"[ \t]+", " ", INVISIBLE.sub("", body)).strip()
    h = _headers(msg)
    return {
        "from": h.get("From", ""),
        "subject": h.get("Subject", ""),
        "date": _local_date(h.get("Date", ""), ctx),
        "body": body[:MAX_BODY],
        "truncated": len(body) > MAX_BODY,
        "attachments": _attachments(message_id, payload, ctx),
    }
