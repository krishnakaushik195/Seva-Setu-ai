# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import io
import re
import hmac
import random
import secrets
import hashlib
import unicodedata
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)

import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort)
from fpdf import FPDF
from PIL import Image
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
# Canonical URLs of the two MCP servers; each is the audience of the tokens it accepts.
MCP_URLS = {"citizen": PUBLIC_BASE_URL + "/mcp/citizen", "officer": PUBLIC_BASE_URL + "/mcp/officer"}

app = Flask(__name__)
# The key that used to sit in app.ini shipped with the code, so anyone could forge an officer session.
app.secret_key = os.environ.get("SECRET_KEY", "")
if len(app.secret_key) < 32:
    raise RuntimeError("SECRET_KEY must be set to a random value of at least 32 characters")
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=PUBLIC_BASE_URL.startswith("https://"),
                  MAX_CONTENT_LENGTH=8 * 1024 * 1024)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_FULL_NAME = os.environ.get("ADMIN_FULL_NAME", "District Social Welfare Officer")

# An applicant may hold at most one application in these states at a time.
# WITHDRAWN and REJECTED applications do not block a fresh application.
ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
# The window is fixed by government order; a deployment can carry the current order via the environment.
SCHEME_DEADLINE = datetime.strptime(os.environ.get("SCHEME_DEADLINE") or
                                    config.get("pension", "scheme_deadline"), "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
MAX_AGE = 120
SLA_DAYS = config.getint("pension", "sla_days")
OTP_MAX_ATTEMPTS = 5
MAX_DOC_BYTES = 5 * 1024 * 1024

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]
# Villages on record per block; in the department's data every village belongs to exactly one block.
VILLAGES = {
    "Sonari": ["Bhekulajan", "Kakotibari", "Lakhipur", "Namdang"],
    "Rajapara": ["Dighaljan", "Rajapara Gaon", "Teliagaon"],
    "Dhemaji Pathar": ["Bordoloni", "Kathoni", "Nalbari Gaon"],
    "Borgaon": ["Borgaon Centre", "Milanpur", "Udaypur"],
    "Namti": ["Hatipara", "Namti Chariali", "Panidihing"],
    "Khelua": ["Chengelimara", "Khelua Gaon", "Sialmari"],
}
VILLAGE_BLOCK = {v.lower(): (v, b) for b, vs in VILLAGES.items() for v in vs}
GENDERS = ("Male", "Female", "Other")
MARITAL_STATUSES = ("Married", "Unmarried", "Widowed")
IFSC_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    return psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=config.get("database", "password"),
    )


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no(cur):
    # Six random digits a year collide with numbers already issued (~58k for 2026), so check.
    for _ in range(50):
        app_no = "SSP%s%06d" % (now_ist().strftime("%y"), 100000 + secrets.randbelow(900000))
        cur.execute("SELECT 1 FROM applications WHERE application_no = %s", (app_no,))
        if cur.fetchone() is None:
            return app_no
    raise RuntimeError("could not allocate an application number")


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (app_id, action, actor, (note or "")[:500], datetime.now()))


def normalize_mobile(raw):
    """A 10-digit Indian mobile number, or None. Accepts +91 / 0 prefixes, spaces and dashes."""
    digits = re.sub(r"[\s\-()]", "", raw or "")
    if digits.startswith("+91"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def issue_otp(mobile):
    code = "%06d" % secrets.randbelow(1000000)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                (mobile, code, datetime.now()))
    conn.commit()
    cur.close(); conn.close()
    send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)


def check_otp(mobile, code):
    """Check the latest OTP sent to `mobile`: valid 5 minutes, single use, at most 5 tries.

    Returns (status, message); status is one of ok, invalid, expired, used, locked, none.
    """
    code = (code or "").strip()
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, code, created_at, attempts, used_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        if not row:
            return "none", "Please request an OTP first."
        otp_id, real, created, attempts, used_at = row
        if used_at is not None:
            return "used", "This OTP has already been used. Please request a new OTP."
        if (datetime.now() - created).total_seconds() > OTP_VALIDITY_SECONDS:
            return "expired", "OTP expired. Please request a new OTP."
        if (attempts or 0) >= OTP_MAX_ATTEMPTS:
            return "locked", "Too many wrong attempts. Please request a new OTP."
        if not hmac.compare_digest(real.encode(), code.encode()):
            cur.execute("UPDATE otps SET attempts = coalesce(attempts, 0) + 1 WHERE id = %s", (otp_id,))
            conn.commit()
            return "invalid", "Invalid OTP."
        cur.execute("UPDATE otps SET used_at = %s WHERE id = %s AND used_at IS NULL",
                    (datetime.now(), otp_id))
        conn.commit()
        if cur.rowcount != 1:
            return "used", "This OTP has already been used. Please request a new OTP."
        return "ok", ""
    finally:
        cur.close(); conn.close()


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def clean_text(value):
    return " ".join(unicodedata.normalize("NFC", value or "").split())


def _choice(value, options):
    return next((o for o in options if o.lower() == value.lower()), None)


# Zero-width (non-)joiners are part of how Assamese and Bangla names are written.
_NAME_PUNCT = set(" .'-") | {chr(0x200C), chr(0x200D)}


def name_problem(value, label):
    """Names are letters in any script (Assamese marks included) with . ' - and spaces.

    The limits also keep free text, such as instructions aimed at an officer's assistant, out of the field.
    """
    if len(value) > 60:
        return "%s is too long (at most 60 characters)" % label
    if len(value.split()) > 6:
        return "%s has too many words; give it exactly as printed on the document" % label
    if not any(unicodedata.category(c).startswith("L") for c in value):
        return "%s must contain letters" % label
    if any(not (unicodedata.category(c)[0] in "LM" or c in _NAME_PUNCT) for c in value):
        return "%s can contain only letters, spaces, dots, apostrophes and hyphens" % label
    return None


def place_problem(value):
    if len(value) > 60 or len(value.split()) > 6:
        return "village name is too long"
    if not any(unicodedata.category(c).startswith("L") for c in value):
        return "village name must contain letters"
    if any(not (unicodedata.category(c)[0] in "LMN" or c in _NAME_PUNCT or c in ",()/") for c in value):
        return "village name can contain only letters, digits, spaces and . ' - , ( ) /"
    return None


def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid);
    `cleaned` holds the normalised values ready for create_application().
    """
    errors = []
    cleaned = {
        "applicant_name": clean_text(data.get("applicant_name")),
        "village": clean_text(data.get("village")),
        "block": clean_text(data.get("block")),
        "gender": clean_text(data.get("gender")),
        "marital_status": clean_text(data.get("marital_status")),
        "husband_name": clean_text(data.get("husband_name")),
        "husband_employer": "",  # no longer collected (not a scheme requirement)
        "bank_account": re.sub(r"[\s-]", "", data.get("bank_account") or ""),
        "ifsc": re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper(),
        "doc_path": doc_path or "",
        "dob": None,
    }
    # IFSC is required: the pension is paid by DBT, which needs it (see the About page).
    for label, key in (("full name", "applicant_name"), ("village", "village"),
                       ("block", "block"), ("bank account", "bank_account"), ("IFSC", "ifsc")):
        if not cleaned[key]:
            errors.append("%s is required" % label)
    if cleaned["applicant_name"]:
        problem = name_problem(cleaned["applicant_name"], "full name")
        if problem:
            errors.append(problem)
    if cleaned["block"]:
        block = _choice(cleaned["block"], BLOCKS)
        if block is None:
            errors.append("block must be one of: " + ", ".join(BLOCKS))
        else:
            cleaned["block"] = block
    if cleaned["village"]:
        problem = place_problem(cleaned["village"])
        if problem:
            errors.append(problem)
        known = VILLAGE_BLOCK.get(cleaned["village"].lower())
        if known:
            cleaned["village"] = known[0]
            if cleaned["block"] in BLOCKS and known[1] != cleaned["block"]:
                errors.append("%s village is in %s block, not %s block"
                              % (known[0], known[1], cleaned["block"]))
    if cleaned["gender"]:
        gender = _choice(cleaned["gender"], GENDERS)
        if gender is None:
            errors.append("gender must be one of: " + ", ".join(GENDERS))
        else:
            cleaned["gender"] = gender
    if cleaned["marital_status"]:
        marital = _choice(cleaned["marital_status"], MARITAL_STATUSES)
        if marital is None:
            errors.append("marital status must be one of: " + ", ".join(MARITAL_STATUSES))
        else:
            cleaned["marital_status"] = marital
    if cleaned["marital_status"] != "Widowed":
        cleaned["husband_name"] = ""
    elif cleaned["husband_name"]:
        problem = name_problem(cleaned["husband_name"], "late husband's name")
        if problem:
            errors.append(problem)
    if cleaned["bank_account"]:
        if not re.fullmatch(r"\d{9,18}", cleaned["bank_account"]):
            errors.append("bank account number must be 9 to 18 digits")
        elif len(set(cleaned["bank_account"])) == 1:
            errors.append("bank account number does not look right; please check the passbook")
    if cleaned["ifsc"] and not IFSC_RE.fullmatch(cleaned["ifsc"]):
        errors.append("IFSC must be 11 characters: 4 letters, the digit 0, then 6 letters or "
                      "digits (e.g. SBIN0003077); please read it from the passbook or cheque")
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        today = now_ist().date()
        if dob > today:
            errors.append("date of birth cannot be in the future")
        elif age_on(dob, today) < MIN_AGE:
            errors.append("applicant must be %d years of age or above" % MIN_AGE)
        elif age_on(dob, today) > MAX_AGE:
            errors.append("date of birth gives an age above %d; please check it" % MAX_AGE)
    if not cleaned["doc_path"]:
        errors.append("age proof document is required")
    if now_ist() > SCHEME_DEADLINE:
        errors.append("the application window has closed")
    return errors, cleaned


def active_application(cur, mobile):
    """The applicant's current live application, if any."""
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN %s "
                "ORDER BY submitted_at DESC LIMIT 1", (mobile, ACTIVE_STATUSES))
    return cur.fetchone()


def lock_mobile(cur, mobile):
    """Serialise submissions for one mobile so two channels cannot both pass the one-live-application check."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("mobile:" + mobile,))


def duplicate_problems(cur, cleaned, mobile):
    """One pension per person, whichever mobile number is used to apply."""
    problems = []
    cur.execute("SELECT 1 FROM applications WHERE bank_account = %s AND ifsc = %s AND mobile <> %s "
                "AND status IN %s LIMIT 1",
                (cleaned["bank_account"], cleaned["ifsc"], mobile, ACTIVE_STATUSES))
    if cur.fetchone():
        problems.append("this bank account is already linked to another live pension application; "
                        "each pensioner needs an account in their own name (for a joint account, "
                        "please apply at the Block Development Office)")
    cur.execute("SELECT 1 FROM applications WHERE dob = %s AND lower(applicant_name) = lower(%s) "
                "AND lower(village) = lower(%s) AND mobile <> %s AND status IN %s LIMIT 1",
                (cleaned["dob"], cleaned["applicant_name"], cleaned["village"], mobile,
                 ACTIVE_STATUSES))
    if cur.fetchone():
        problems.append("a live pension application already exists for a person with the same name, "
                        "date of birth and village; one pension per person (if this is a different "
                        "person, please visit the Block Development Office)")
    return problems


def inspect_document(content):
    """Check an uploaded age-proof file by its content, not its name.

    Returns (problem, info); info carries ext, mime and image size.
    """
    if not content:
        return "the file is empty", None
    if len(content) > MAX_DOC_BYTES:
        return "the file is larger than 5 MB; please upload a smaller file", None
    if content[:5] == b"%PDF-":
        try:
            import pypdfium2
            pages = len(pypdfium2.PdfDocument(content))
        except Exception:
            pages = 0
        if pages < 1:
            return "the PDF file could not be opened; please upload it again", None
        return None, {"ext": "pdf", "mime": "application/pdf", "pages": pages}
    if content[:3] == b"\xff\xd8\xff":
        ext, mime = "jpg", "image/jpeg"
    elif content[:8] == b"\x89PNG\r\n\x1a\n":
        ext, mime = "png", "image/png"
    else:
        return "only JPG, PNG or PDF files are accepted", None
    try:
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
        with Image.open(io.BytesIO(content)) as img:
            width, height = img.size
            low, high = img.convert("L").getextrema()
    except Exception:
        return "the image could not be read; please take the photo again", None
    if min(width, height) < 200:
        return "the image is too small to read; it needs at least 200 pixels on each side", None
    if high - low < 16:
        return "the image appears blank; please photograph the document itself", None
    return None, {"ext": ext, "mime": mime, "width": width, "height": height}


def save_document(mobile, content, ext):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, "%s_%s.%s" % (mobile, secrets.token_hex(8), ext))
    with open(path, "wb") as out:
        out.write(content)
    return path


def create_application(cur, mobile, cleaned, via=""):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no).
    """
    app_no = new_application_no(cur)
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
         cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
         cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
         cleaned["doc_path"], datetime.now()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile, via)
    return new_id, app_no


# ---------------------------------------------------------------------------
# Officers and decisions
# ---------------------------------------------------------------------------

def _blocks_from(spec):
    wanted = {b.strip().lower() for b in (spec or "").split(",")}
    return list(BLOCKS) if "*" in wanted else [b for b in BLOCKS if b.lower() in wanted]


def get_officer(username):
    """{username, full_name, blocks} for an active officer. The admin from the environment covers every block."""
    if username == ADMIN_USERNAME and ADMIN_PASSWORD:
        return {"username": ADMIN_USERNAME, "full_name": ADMIN_FULL_NAME, "blocks": list(BLOCKS)}
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, full_name, blocks FROM officers WHERE username = %s AND active",
                (username,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    return {"username": row[0], "full_name": row[1], "blocks": _blocks_from(row[2])}


def authenticate_officer(username, password):
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return None
    if username == ADMIN_USERNAME:
        if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            return get_officer(username)
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM officers WHERE username = %s AND active", (username,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row and check_password_hash(row[0], password):
        return get_officer(username)
    return None


def approval_blockers(dob, submitted_at, doc_path, ifsc, bank_account):
    """Why the counter would refuse to approve this application; empty when it may be approved."""
    blockers = []
    if dob is None or age_on(dob, submitted_at.date()) < MIN_AGE:
        blockers.append("the applicant was under %d on the date of application" % MIN_AGE)
    if not doc_path:
        blockers.append("no age-proof document is on file")
    if not IFSC_RE.fullmatch(ifsc or ""):
        blockers.append("IFSC '%s' is not a valid 11-character IFSC, so the pension cannot be paid"
                        % (ifsc or ""))
    if not re.fullmatch(r"\d{9,18}", bank_account or ""):
        blockers.append("the bank account number is not valid")
    return blockers


def citizen_reason(note):
    """The officer's reason without the channel tag that the audit trail keeps."""
    return re.sub(r"\s*\[via [^\]]*\]\s*$", "", note or "").strip()


def record_decision(cur, app_id, new_status, officer, note):
    """Approve or reject one PENDING application in the officer's blocks. The caller commits.

    Returns (result, error). Every decision carries a reason and is recorded against the officer's name.
    """
    cur.execute("SELECT application_no, mobile, dob, submitted_at, doc_path, ifsc, bank_account, "
                "block, status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
    row = cur.fetchone()
    if not row:
        return None, "application not found"
    app_no, mobile, dob, submitted_at, doc_path, ifsc, bank, block, status = row
    if block not in officer["blocks"]:
        return None, "application %s is in %s block, which is not one of your blocks" % (app_no, block)
    if status != "PENDING":
        return None, "application %s is %s; only a pending application can be decided" % (app_no, status)
    if not citizen_reason(note):
        return None, "a reason must be recorded with every decision"
    if new_status == "APPROVED":
        blockers = approval_blockers(dob, submitted_at, doc_path, ifsc, bank)
        if blockers:
            return None, "application %s cannot be approved: %s" % (app_no, "; ".join(blockers))
    cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s WHERE id = %s",
                (new_status, datetime.now(), officer["username"], app_id))
    write_audit(cur, app_id, new_status, "admin:%s" % officer["username"], note)
    return {"application_no": app_no, "mobile": mobile, "status": new_status}, None


def notify_decision(result, note):
    if result["status"] == "APPROVED":
        text = ("Sewa Setu: your pension application %s has been approved. Rs. 250 per month will be "
                "paid to your bank account." % result["application_no"])
    else:
        text = ("Sewa Setu: your pension application %s was not approved. Reason: %s. You may apply "
                "again once this is corrected." % (result["application_no"], citizen_reason(note)[:120]))
    send_sms(result["mobile"], text)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining(),
                           closes=SCHEME_DEADLINE.strftime("%I:%M %p, %d %B %Y"))


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        mobile = normalize_mobile(mobile)
        if not mobile:
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        issue_otp(mobile)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        status, message = check_otp(mobile, request.form.get("otp", ""))
        if status == "ok":
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash(message)
        if status in ("expired", "locked", "used", "none"):
            return redirect(url_for("apply"))
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("Please choose a file to upload (JPG, PNG or PDF, up to 5 MB).")
            return render_template("upload.html")
        content = f.read(MAX_DOC_BYTES + 1)
        problem, info = inspect_document(content)
        if problem:
            flash(problem[0].upper() + problem[1:] + ".")
            return render_template("upload.html")
        session["doc_path"] = save_document(session["verified_mobile"], content, info["ext"])
        session["doc_name"] = secure_filename(f.filename) or "document." + info["ext"]
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    # Preview everything before the applicant commits.
    data = session.get("form_data", {})
    errors, _ = validate_application(data, session.get("doc_path", ""))
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=session.get("doc_name") or
                           os.path.basename(session.get("doc_path", "") or ""))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    errors, cleaned = validate_application(data, session.get("doc_path", ""))
    if errors:
        flash("Please correct the following before submitting: " + "; ".join(errors) + ".")
        return redirect(url_for("form_step", step=1))

    conn = get_db()
    cur = conn.cursor()
    lock_mobile(cur, mobile)
    existing = active_application(cur, mobile)
    if existing:
        cur.close(); conn.close()
        flash("Application %s (%s) already exists for this mobile number. A pending "
              "application can be withdrawn from the status portal if you need to "
              "apply afresh." % (existing[1], existing[2]))
        return redirect(url_for("index"))
    duplicates = duplicate_problems(cur, cleaned, mobile)
    if duplicates:
        cur.close(); conn.close()
        flash("This application cannot be submitted: " + "; ".join(duplicates) + ".")
        return redirect(url_for("index"))

    new_id, app_no = create_application(cur, mobile, cleaned)
    conn.commit()
    cur.close(); conn.close()

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("doc_name", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    reason = ""
    if row and row[8] == "REJECTED":
        cur.execute("SELECT note FROM audit_log WHERE application_id = %s AND action = 'REJECTED' "
                    "ORDER BY at DESC LIMIT 1", (app_id,))
        r = cur.fetchone()
        reason = citizen_reason(r[0]) if r else ""
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id, reason=reason)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own PENDING application so they can apply afresh."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
        flash("Application %s has been withdrawn. You may submit a new application." % row[0])
    else:
        flash("Only a pending application can be withdrawn.")
    cur.close(); conn.close()
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            # the latest application's DOB, matching what the status portal shows after login
            cur.execute("SELECT dob FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        officer = authenticate_officer(request.form.get("username"), request.form.get("password"))
        if officer:
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = officer["username"]
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


def current_officer():
    """The officer signed in to the web admin pages, or None."""
    if not session.get("admin"):
        return None
    return get_officer(session.get("admin_user", ""))


@app.route("/admin/dashboard")
def admin_dashboard():
    officer = current_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    blocks = officer["blocks"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications WHERE block = ANY(%s) GROUP BY status",
                (blocks,))
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' AND block = ANY(%s) "
                "AND submitted_at < %s", (blocks, datetime.now() - timedelta(days=SLA_DAYS)))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "AND block = ANY(%s) GROUP BY block ORDER BY count(*) DESC", (blocks,))
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS, officer=officer)


@app.route("/admin/applications")
def admin_list():
    officer = current_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    mobile = request.args.get("mobile", "").strip()
    page = request.args.get("page", "1")
    page = max(1, int(page)) if page.isdigit() else 1
    conn = get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE mobile = %s AND block = ANY(%s) "
                    "ORDER BY submitted_at ASC", (mobile, officer["blocks"]))
        status = "mobile %s" % mobile
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s AND block = ANY(%s) "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, officer["blocks"], (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page, mobile=mobile)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    officer = current_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s AND block = ANY(%s)", (app_id, officer["blocks"]))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    cur.close(); conn.close()
    blockers = approval_blockers(row[3], row[12], row[10], row[9], row[8]) if row[11] == "PENDING" else []
    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit, blockers=blockers,
                           doc_name=os.path.basename(row[10] or ""))


@app.route("/admin/application/<int:app_id>/document")
def admin_document(app_id):
    officer = current_officer()
    if not officer:
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT doc_path FROM applications WHERE id = %s AND block = ANY(%s)",
                (app_id, officer["blocks"]))
    row = cur.fetchone()
    cur.close(); conn.close()
    path = os.path.realpath(row[0]) if row and row[0] else ""
    if not path.startswith(os.path.realpath(UPLOAD_DIR) + os.sep) or not os.path.exists(path):
        abort(404)
    return send_file(path)


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided, when and why."""
    officer = current_officer()
    if not officer:
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    result, error = record_decision(cur, app_id, new_status, officer, (note or "").strip())
    if result:
        conn.commit()
        notify_decision(result, note)
        flash("Application %s %s." % (result["application_no"], new_status.lower()))
    else:
        conn.rollback()
        flash(error[0].upper() + error[1:] + ".")
    cur.close(); conn.close()
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)
from agent_web import agent_web as _agent_web_blueprint   # noqa: E402
app.register_blueprint(_agent_web_blueprint)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
