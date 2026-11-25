import hashlib
import hmac
import html
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.id import ID
from appwrite.query import Query
from appwrite.services.tables_db import TablesDB

# ---------- Config (set as function variables) ----------
DATABASE_ID = os.environ.get("DATABASE_ID", "6b0663c10013c539ce08")
USER_TABLE_ID = os.environ.get("USER_TABLE_ID", "6b067b6f002d5ecc6f1c")
BOOKING_TABLE_ID = os.environ.get("BOOKING_TABLE_ID", "6b0663d80015144b2faf")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
APPROVAL_SECRET = os.environ.get("APPROVAL_SECRET", "")  # long random string
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # this function's domain, e.g. https://abc.appwrite.global

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)

USER_FIELDS = ["THNAME", "THSRNAME", "NAME", "SRNAME", "AFF", "ROLE"]
ROLES = {"student", "teacher"}


# ---------- Helpers ----------
def get_tables(context):
    client = (
        Client()
        .set_endpoint(os.environ["APPWRITE_FUNCTION_API_ENDPOINT"])
        .set_project(os.environ["APPWRITE_FUNCTION_PROJECT_ID"])
        .set_key(context.req.headers["x-appwrite-key"])
    )
    return TablesDB(client)


def sign(booking_id, action):
    return hmac.new(
        APPROVAL_SECRET.encode(), f"{booking_id}:{action}".encode(), hashlib.sha256
    ).hexdigest()


def page(context, title, body, status=200):
    doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;padding:0 16px'>"
        f"<h2>{html.escape(title)}</h2>{body}</body></html>"
    )
    return context.res.text(doc, status, {"content-type": "text/html; charset=utf-8"})


def send_email(subject, text):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ADMIN_EMAIL
    msg.set_content(text)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USERNAME, SMTP_PASSWORD)
        s.send_message(msg)


def to_utc_iso(value):
    """Normalise an ISO date/datetime string to UTC ISO so equality checks work."""
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


# ---------- Route 1: POST /users ----------
def add_user(context, tables):
    body = context.req.body_json or {}
    # Prefer the authenticated caller's id (set by Appwrite); fall back to body.
    user_id = context.req.headers.get("x-appwrite-user-id") or body.get("USERID")
    if not user_id:
        return context.res.json({"error": "USERID missing"}, 400)

    missing = [f for f in USER_FIELDS if not str(body.get(f, "")).strip()]
    if missing:
        return context.res.json({"error": f"Missing fields: {', '.join(missing)}"}, 400)
    if body["ROLE"].lower() not in ROLES:
        return context.res.json({"error": "ROLE must be 'student' or 'teacher'"}, 400)

    data = {f: str(body[f]).strip() for f in USER_FIELDS}
    data["ROLE"] = data["ROLE"].lower()
    data["USERID"] = user_id

    try:
        # Row id = USERID so each user has exactly one row.
        row = tables.create_row(DATABASE_ID, USER_TABLE_ID, user_id, data)
        return context.res.json({"ok": True, "row": row["$id"]}, 201)
    except AppwriteException as e:
        if e.code == 409:  # already registered -> update profile
            tables.update_row(DATABASE_ID, USER_TABLE_ID, user_id, data)
            return context.res.json({"ok": True, "row": user_id, "updated": True})
        raise


# ---------- Route 2: POST /bookings ----------
def book_room(context, tables):
    body = context.req.body_json or {}
    user_id = context.req.headers.get("x-appwrite-user-id") or body.get("USERID")
    if not user_id:
        return context.res.json({"error": "USERID missing"}, 400)

    room = str(body.get("ROOM_BOOKED", "")).strip()
    period = str(body.get("PERIOD", "")).strip()
    if not room or not period or not body.get("BOOKED_DATE"):
        return context.res.json(
            {"error": "ROOM_BOOKED, PERIOD and BOOKED_DATE are required"}, 400
        )
    try:
        booked_date = to_utc_iso(body["BOOKED_DATE"])
    except ValueError:
        return context.res.json({"error": "BOOKED_DATE must be an ISO date"}, 400)

    # Reject if someone already holds this slot (pending or confirmed).
    taken = tables.list_rows(
        DATABASE_ID,
        BOOKING_TABLE_ID,
        [
            Query.equal("ROOM_BOOKED", [room]),
            Query.equal("PERIOD", [period]),
            Query.equal("BOOKED_DATE", [booked_date]),
            Query.not_equal("STATUS", ["rejected"]),
            Query.limit(1),
        ],
    )
    if taken["total"] > 0:
        return context.res.json({"error": "Slot already requested or booked"}, 409)

    booking = tables.create_row(
        DATABASE_ID,
        BOOKING_TABLE_ID,
        ID.unique(),
        {
            "ROOM_BOOKED": room,
            "PERIOD": period,
            "STATUS": "pending",
            "BOOKED_DATE": booked_date,
            "USERID": user_id,
        },
    )

    # Look up requester for the email (best effort).
    try:
        u = tables.get_row(DATABASE_ID, USER_TABLE_ID, user_id)
        who = (
            f"{u.rows['NAME']} {u.rows['SRNAME']} ({u.rows['THNAME']} {u.rows['THSRNAME']})\n"
            f"Role: {u.rows['ROLE']}\nAffiliation: {u.rows['AFF']}"
        )
    except AppwriteException:
        who = f"User id: {user_id} (no profile found)"

    base = (PUBLIC_URL or f"https://{context.req.host}").rstrip("/")

    def link(action):
        q = urlencode({"id": booking["$id"], "action": action, "sig": sign(booking["$id"], action)})
        return f"{base}/approve?{q}"

    try:
        send_email(
            f"Room booking request: {room} period {period}",
            f"New room booking request\n\n"
            f"Room: {room}\nPeriod: {period}\nDate: {booked_date}\n\n"
            f"Requested by:\n{who}\n\n"
            f"Approve: {link('approve')}\n"
            f"Reject:  {link('reject')}\n",
        )
    except Exception as e:
        context.error(f"Email failed: {e}")

    return context.res.json({"ok": True, "booking": booking["$id"], "STATUS": "pending"}, 201)


# ---------- Route 3: GET /approve (no auth, signed link) ----------
def approve(context, tables):
    q = context.req.query or {}
    booking_id, action, sig = q.get("id"), q.get("action"), q.get("sig", "")

    if action not in ("approve", "reject") or not booking_id:
        return page(context, "Invalid link", "<p>This link is not valid.</p>", 400)
    if not APPROVAL_SECRET or not hmac.compare_digest(sig, sign(booking_id, action)):
        return page(context, "Invalid link", "<p>Signature check failed.</p>", 403)

    try:
        row = tables.get_row(DATABASE_ID, BOOKING_TABLE_ID, booking_id)
    except AppwriteException:
        return page(context, "Not found", "<p>Booking not found.</p>", 404)

    summary = (
        f"<p><b>{html.escape(row['ROOM_BOOKED'])}</b>, period {html.escape(row['PERIOD'])}<br>"
        f"{html.escape(row['BOOKED_DATE'])}<br>Current status: {html.escape(row['STATUS'])}</p>"
    )

    # Step 1: show a confirm button, so email link-scanners that prefetch URLs don't change anything.
    if q.get("confirm") != "1":
        params = "".join(
            f"<input type='hidden' name='{k}' value='{html.escape(v)}'>"
            for k, v in {"id": booking_id, "action": action, "sig": sig, "confirm": "1"}.items()
        )
        label = "Approve" if action == "approve" else "Reject"
        return page(
            context,
            f"{label} this booking?",
            f"{summary}<form method='GET' action='/approve'>{params}"
            f"<button style='padding:8px 16px;font-size:1rem'>{label}</button></form>",
        )

    # Step 2: apply.
    new_status = "approved" if action == "approve" else "rejected"
    tables.update_row(DATABASE_ID, BOOKING_TABLE_ID, booking_id, {"STATUS": new_status})
    return page(context, f"Booking {new_status}", summary.replace(row["STATUS"], new_status))


# ---------- Entry point ----------
def main(context):
    tables = get_tables(context)
    path = context.req.path.rstrip("/") or "/"
    method = context.req.method

    try:
        if path == "/users" and method == "POST":
            return add_user(context, tables)
        if path == "/bookings" and method == "POST":
            return book_room(context, tables)
        if path == "/approve" and method == "GET":
            return approve(context, tables)
        return context.res.json({"error": "Not found"}, 404)
    except AppwriteException as e:
        context.error(f"Appwrite error: {e.message}")
        return context.res.json({"error": e.message}, e.code or 500)
    except Exception as e:
        context.error(str(e))
        return context.res.json({"error": "Internal error"+str(e)}, 500)
