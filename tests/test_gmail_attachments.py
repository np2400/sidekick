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


JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 10_000  # 50 KB: the size Gmail reports for map.jpg


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


@pytest.fixture
def mailbox(monkeypatch):
    """A fake Gmail: one email with a text body, a PDF voucher (fetched separately) and a photo."""
    attachments = {"att-pdf": tiny_pdf("Hotel Snow Lion, Tawang. Check-in Dec 12, check-out Dec 14."),
                   "att-jpg": JPEG}
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
    assert photo == {"attachment": 2, "filename": "map.jpg", "note": f"an image: {gmail.LOOK}"}


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


# --- Images, and PDFs whose pages are images (look at them with view_attachment) ------------------------------------

def test_images_pasted_into_the_body_are_listed_but_logos_are_not_offered(mailbox):
    parts = mailbox["message"]["payload"]["parts"]
    parts.append({"mimeType": "image/png", "filename": "", "body": {"attachmentId": "att-ticket", "size": 80_000}})
    parts.append({"mimeType": "image/png", "filename": "logo.png", "body": {"attachmentId": "att-logo", "size": 900}})
    parts.append({"mimeType": "application/zip", "filename": "photos.zip", "body": {"attachmentId": "z", "size": 9}})
    *_, ticket, logo, archive = gmail.read_email("m9")["attachments"]
    assert ticket == {"attachment": 3, "filename": "image 3 in the email", "note": f"an image: {gmail.LOOK}"}
    assert logo["note"] == "not shown (a small image, like a logo)"
    assert archive["note"] == "not read (only PDFs and images are)"


def test_view_attachment_returns_the_image_itself_not_text(mailbox):
    shown = gmail.view_attachment("m9", 2)
    assert shown["summary"].startswith("map.jpg") and len(shown["_images"]) == 1
    assert shown["_images"][0].mime == "image/jpeg" and shown["_images"][0].data == JPEG
    assert shown["_images"][0].from_email  # routed to the model that keeps nothing first

    assert gmail.view_attachment("m9", 7)["error"] == "That email has no attachment 7 (it has 2)."
    mailbox["message"]["payload"]["parts"][2]["body"]["size"] = 500  # a logo
    assert "logo-sized" in gmail.view_attachment("m9", 2)["error"]


def test_a_pdf_whose_pages_are_images_is_offered_and_shown_as_a_pdf_of_at_most_six_pages(mailbox):
    import io

    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for _ in range(9):
        writer.add_blank_page(612, 792)  # no text: like a scanned ticket
    scan = io.BytesIO()
    writer.write(scan)
    mailbox["attachments"]["att-pdf"] = scan.getvalue()

    assert gmail.read_email("m9")["attachments"][0]["note"] == f"no text in this PDF (its pages are images, e.g. a scan): {gmail.LOOK}"
    [pdf] = gmail.view_attachment("m9", 1)["_images"]
    assert pdf.mime == "application/pdf" and len(PdfReader(io.BytesIO(pdf.data)).pages) == gmail.MAX_PDF_PAGES
