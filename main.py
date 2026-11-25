import hashlib
import hmac
import html
import os
import smtplib
import traceback
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
#DEBUG = os.environ.get("DEBUG") == "1"
DEBUG = True

# ---------- Helpers ----------
def get_tables(context):
    client = (
        Client()
        .set_endpoint(os.environ["APPWRITE_FUNCTION_API_ENDPOINT"])
        .set_project(os.environ["APPWRITE_FUNCTION_PROJECT_ID"])
        .set_key(context.req.headers["x-appwrite-key"])
    )
    return TablesDB(client)


def as_dict(x):
    """SDK results are dicts in old versions, model objects (Row, RowList) in new ones."""
    if isinstance(x, dict):
        d = x
    elif hasattr(x, "to_dict"):
        d = x.to_dict()
    else:
        d = dict(vars(x))
    data = d.get("data")
    if isinstance(data, dict):  # new SDKs nest the columns under "data"
        d = {**data, **{k: v for k, v in d.items() if k != "data"}}
    return d


def rows_of(result):
    d = as_dict(result)
    return [as_dict(r) for r in (d.get("rows") or d.get("documents") or [])]


def slot_rows(tables, room, period, booked_date, status):
    """All bookings for one room/period/date with the given status."""
    return rows_of(
        tables.list_rows(
            DATABASE_ID,
            BOOKING_TABLE_ID,
            [
                Query.equal("ROOM_BOOKED", [room]),
                Query.equal("PERIOD", [period]),
                Query.equal("BOOKED_DATE", [booked_date]),
                Query.equal("STATUS", [status]),
                Query.limit(100),
            ],
        )
    )


def row_id(d):
    return d.get("$id") or d.get("id")


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


def send_email(context, subject, text):
    """Sends the email and logs the outcome. Returns (ok, error_message)."""
    required = {
        "ADMIN_EMAIL": ADMIN_EMAIL, "SMTP_HOST": SMTP_HOST, "SMTP_USERNAME": SMTP_USERNAME,
        "SMTP_PASSWORD": SMTP_PASSWORD, "SMTP_FROM": SMTP_FROM,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        err = f"Missing function variables: {', '.join(missing)}"
        context.error(f"[email] {err}")
        return False, err

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ADMIN_EMAIL
    msg.set_content(text)

    context.log(f"[email] sending to {ADMIN_EMAIL} via {SMTP_HOST}:{SMTP_PORT}")
    try:
        if SMTP_PORT == 465:  # implicit TLS
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
            server.starttls()
        with server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        context.error(f"[email] FAILED: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return False, f"{type(e).__name__}: {e}"

    context.log("[email] sent OK")
    return True, None


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
        tables.create_row(DATABASE_ID, USER_TABLE_ID, user_id, data)
        return context.res.json({"ok": True, "row": user_id}, 201)
    except AppwriteException as e:
        if e.code == 409:  # already registered -> update profile
            tables.update_row(DATABASE_ID, USER_TABLE_ID, user_id, data)
            return context.res.json({"ok": True, "row": user_id, "updated": True})
        raise


# ---------- Route 1b: GET /users/me ----------
def get_me(context, tables):
    user_id = context.req.headers.get("x-appwrite-user-id")
    if not user_id:
        return context.res.json({"error": "Not logged in"}, 401)
    try:
        row = as_dict(tables.get_row(DATABASE_ID, USER_TABLE_ID, user_id))
    except AppwriteException as e:
        if e.code == 404:
            return context.res.json({"error": "Not registered"}, 404)
        raise
    return context.res.json({"ok": True, "row": {k: row[k] for k in USER_FIELDS}})


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

    # Pending requests may stack up; only an approved booking blocks the slot.
    if slot_rows(tables, room, period, booked_date, "approved"):
        return context.res.json({"error": "This slot is already booked."}, 409)
    if any(r["USERID"] == user_id for r in slot_rows(tables, room, period, booked_date, "pending")):
        return context.res.json({"error": "You already requested this slot."}, 409)

    booking = as_dict(tables.create_row(
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
    ))
    booking_id = row_id(booking)

    # Look up requester for the email (best effort).
    try:
        u = as_dict(tables.get_row(DATABASE_ID, USER_TABLE_ID, user_id))
        who = (
            f"{u['NAME']} {u['SRNAME']} ({u['THNAME']} {u['THSRNAME']})\n"
            f"Role: {u['ROLE']}\nAffiliation: {u['AFF']}"
        )
    except AppwriteException:
        who = f"User id: {user_id} (no profile found)"

    base = (PUBLIC_URL or f"https://{context.req.host}").rstrip("/")

    def link(action):
        q = urlencode({"id": booking_id, "action": action, "sig": sign(booking_id, action)})
        return f"{base}/approve?{q}"

    others = [
        r for r in slot_rows(tables, room, period, booked_date, "pending") if row_id(r) != booking_id
    ]
    email_ok, email_err = send_email(
        context,
        f"Room booking request: {room} period {period}",
        f"New room booking request\n\n"
        f"Room: {room}\nPeriod: {period}\nDate: {booked_date}\n\n"
        f"Requested by:\n{who}\n\n"
        f"Other pending requests for this slot: {len(others)}\n\n"
        f"Approve:                          {link('approve')}\n"
        f"Approve + reject other requests:  {link('approve_reject_others')}\n"
        f"Reject:                           {link('reject')}\n",
    )
    context.log(f"[booking] {booking_id} created, emailSent={email_ok}")

    result = {"ok": True, "booking": booking_id, "STATUS": "pending", "emailSent": email_ok}
    if DEBUG and email_err:
        result["emailError"] = email_err
    return context.res.json(result, 201)


# ---------- Route 2b: GET /bookings?room=... ----------
def list_bookings(context, tables):
    room = str((context.req.query or {}).get("room", "")).strip()
    if not room:
        return context.res.json({"error": "room is required"}, 400)

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = rows_of(
        tables.list_rows(
            DATABASE_ID,
            BOOKING_TABLE_ID,
            [
                Query.equal("ROOM_BOOKED", [room]),
                Query.not_equal("STATUS", ["rejected"]),
                Query.greater_than_equal("BOOKED_DATE", today.isoformat()),
                Query.limit(200),
            ],
        )
    )

    # Look up the bookers' profiles (in chunks to keep queries small).
    ids = sorted({r["USERID"] for r in rows})
    people = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        for u in rows_of(
            tables.list_rows(
                DATABASE_ID, USER_TABLE_ID, [Query.equal("USERID", chunk), Query.limit(len(chunk))]
            )
        ):
            people[u["USERID"]] = {
                "name": f"{u.get('NAME', '')} {u.get('SRNAME', '')}".strip(),
                "role": u.get("ROLE", ""),
                "aff": u.get("AFF", ""),
            }

    return context.res.json(
        {
            "bookings": [
                {
                    "date": to_utc_iso(r["BOOKED_DATE"])[:10],
                    "period": str(r["PERIOD"]),
                    "status": r["STATUS"],
                    "booker": people.get(r["USERID"]) if r["STATUS"] == "approved" else None,
                }
                for r in rows
            ]
        }
    )


# ---------- Route 3: GET /approve (no auth, signed link) ----------
ACTIONS = ("approve", "reject", "approve_reject_others")


def approve(context, tables):
    q = context.req.query or {}
    booking_id, action, sig = q.get("id"), q.get("action"), q.get("sig", "")

    if action not in ACTIONS or not booking_id:
        return page(context, "Invalid link", "<p>This link is not valid.</p>", 400)
    if not APPROVAL_SECRET or not hmac.compare_digest(sig, sign(booking_id, action)):
        return page(context, "Invalid link", "<p>Signature check failed.</p>", 403)

    try:
        row = as_dict(tables.get_row(DATABASE_ID, BOOKING_TABLE_ID, booking_id))
    except AppwriteException:
        return page(context, "Not found", "<p>Booking not found.</p>", 404)

    def summary(status):
        return (
            f"<p><b>{html.escape(row['ROOM_BOOKED'])}</b>, period {html.escape(str(row['PERIOD']))}<br>"
            f"{html.escape(str(row['BOOKED_DATE']))}<br>Status: {html.escape(status)}</p>"
        )

    room, period = row["ROOM_BOOKED"], str(row["PERIOD"])
    slot_date = to_utc_iso(row["BOOKED_DATE"])
    others = [r for r in slot_rows(tables, room, period, slot_date, "pending") if row_id(r) != booking_id]

    if action != "reject":
        taken = [r for r in slot_rows(tables, room, period, slot_date, "approved") if row_id(r) != booking_id]
        if taken:
            return page(
                context, "Slot already approved",
                summary(row["STATUS"]) + "<p>Another request for this slot is already approved.</p>", 409,
            )

    # Step 1: confirm button, so email link-scanners that prefetch URLs don't change anything.
    if q.get("confirm") != "1":
        label = {
            "approve": "Approve",
            "reject": "Reject",
            "approve_reject_others": f"Approve and reject {len(others)} other request(s)",
        }[action]
        params = "".join(
            f"<input type='hidden' name='{k}' value='{html.escape(v)}'>"
            for k, v in {"id": booking_id, "action": action, "sig": sig, "confirm": "1"}.items()
        )
        return page(
            context,
            label + "?",
            f"{summary(row['STATUS'])}<form method='GET' action='/approve'>{params}"
            f"<button style='padding:8px 16px;font-size:1rem'>{html.escape(label)}</button></form>",
        )

    # Step 2: apply.
    new_status = "rejected" if action == "reject" else "approved"
    tables.update_row(DATABASE_ID, BOOKING_TABLE_ID, booking_id, {"STATUS": new_status})
    rejected = 0
    if action == "approve_reject_others":
        for r in others:
            tables.update_row(DATABASE_ID, BOOKING_TABLE_ID, row_id(r), {"STATUS": "rejected"})
            rejected += 1
    context.log(f"[approve] {booking_id} -> {new_status}; also rejected {rejected} other request(s)")
    extra = f"<p>{rejected} other request(s) for this slot were rejected.</p>" if rejected else ""
    return page(context, f"Booking {new_status}", summary(new_status) + extra)


# ---------- Entry point ----------
def main(context):
    tables = get_tables(context)
    path = context.req.path.rstrip("/") or "/"
    method = context.req.method

    try:
        if path == "/users/me" and method == "GET":
            return get_me(context, tables)
        if path == "/users" and method == "POST":
            return add_user(context, tables)
        if path == "/bookings" and method == "GET":
            return list_bookings(context, tables)
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
        return context.res.json({"error": "Internal error"}, 500)
