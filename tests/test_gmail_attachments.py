"""read_email includes PDF attachments (itineraries, vouchers), within limits. Gmail is faked; the PDF is real."""
import base64

import pytest

from app import gmail


def tiny_pdf(text: str) -> bytes:
    """A minimal one-page PDF with one line of text, built by hand so the test needs no PDF-writing library."""
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


@pytest.fixture
def mailbox(monkeypatch):
    """A fake Gmail: one email with a text body, a PDF voucher (fetched separately) and a photo."""
    attachments = {"att-pdf": tiny_pdf("Hotel Snow Lion, Tawang. Check-in Dec 12, check-out Dec 14.")}
    message = {"id": "m9", "payload": {"mimeType": "multipart/mixed", "headers": [
        {"name": "From", "value": "Himalayan Trails <tours@trails.example>"},
        {"name": "Subject", "value": "Your Arunachal itinerary"}], "parts": [
        {"mimeType": "text/plain", "body": {"data": b64(b"Please find your vouchers attached.")}},
        {"mimeType": "application/pdf", "filename": "vouchers.pdf", "body": {"attachmentId": "att-pdf", "size": 900}},
        {"mimeType": "image/jpeg", "filename": "map.jpg", "body": {"attachmentId": "att-jpg", "size": 50_000}}]}}

    def fake_request(method, url, service, ctx=None, **kwargs):
        if "/attachments/" in url:
            return {"data": b64(attachments[url.rsplit("/", 1)[1]])}
        return message
    monkeypatch.setattr(gmail, "google_request", fake_request)
    return {"message": message, "attachments": attachments}


def test_pdf_attachments_are_read_and_other_files_are_listed(mailbox):
    email = gmail.read_email("m9")
    assert email["body"] == "Please find your vouchers attached."
    pdf, photo = email["attachments"]
    assert pdf["filename"] == "vouchers.pdf" and "Hotel Snow Lion, Tawang" in pdf["text"]
    assert photo == {"filename": "map.jpg", "note": "not read (only PDFs are)"}


def test_broken_or_oversized_pdfs_get_a_note_instead_of_crashing(mailbox):
    mailbox["attachments"]["att-pdf"] = b"%PDF-1.4 this is not really a pdf"
    assert gmail.read_email("m9")["attachments"][0]["note"].startswith("couldn't read this PDF")

    mailbox["message"]["payload"]["parts"][1]["body"]["size"] = gmail.MAX_PDF_BYTES + 1
    assert gmail.read_email("m9")["attachments"][0]["note"] == "not read (too large)"


def test_attachment_text_has_a_budget(mailbox, monkeypatch):
    monkeypatch.setattr(gmail, "MAX_ATTACHMENT_TEXT", 20)
    pdf = gmail.read_email("m9")["attachments"][0]
    assert len(pdf["text"]) == 20 and pdf["truncated"] is True


def test_a_pdf_labelled_as_a_generic_file_is_still_read(mailbox):
    """Some senders attach PDFs as application/octet-stream: the .pdf name and the %PDF bytes decide."""
    mailbox["message"]["payload"]["parts"][1]["mimeType"] = "application/octet-stream"
    assert "Hotel Snow Lion" in gmail.read_email("m9")["attachments"][0]["text"]

    mailbox["attachments"]["att-pdf"] = b"PK\x03\x04 a zip file wearing a .pdf name"
    assert gmail.read_email("m9")["attachments"][0]["note"] == "not read (named .pdf but isn't a PDF)"


def test_a_pdf_whose_pages_are_pictures_says_so_instead_of_returning_empty_text(mailbox):
    mailbox["attachments"]["att-pdf"] = tiny_pdf("")
    pdf = gmail.read_email("m9")["attachments"][0]
    assert "text" not in pdf and pdf["note"].startswith("no text in this PDF")
