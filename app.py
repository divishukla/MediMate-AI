import os
import sqlite3
import secrets
import hashlib
import hmac
import smtplib
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    BackgroundScheduler = None

app = Flask(__name__)
app.secret_key = os.environ.get("MEDIMATE_SECRET_KEY", secrets.token_hex(32))
DB_PATH = os.environ.get("MEDIMATE_DB", os.path.join(os.path.dirname(__file__), "medimate.db"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS medications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          instructions TEXT DEFAULT '',
          frequency TEXT DEFAULT 'As prescribed',
          times TEXT NOT NULL,
          notes TEXT DEFAULT '',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS appointments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          doctor TEXT DEFAULT '',
          appt_date TEXT NOT NULL,
          appt_time TEXT DEFAULT '',
          location TEXT DEFAULT '',
          notes TEXT DEFAULT '',
          notified INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminder_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          medication_id INTEGER REFERENCES medications(id) ON DELETE SET NULL,
          medication_name TEXT NOT NULL,
          scheduled_for TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'Pending',
          acknowledged_at TEXT,
          UNIQUE(user_id, medication_id, scheduled_for)
        );
        CREATE TABLE IF NOT EXISTS caregiver_settings (
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          enabled INTEGER NOT NULL DEFAULT 0,
          name TEXT DEFAULT '',
          email TEXT DEFAULT '',
          consent INTEGER NOT NULL DEFAULT 0,
          after_minutes INTEGER NOT NULL DEFAULT 30
        );
        """)
init_db()

def current_user():
    if "user_id" not in session:
        return None
    with db() as c:
        return c.execute("SELECT id,name,email FROM users WHERE id=?", (session["user_id"],)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def send_email(to_email, subject, body):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM", user or "")
    if not (host and user and password and sender and to_email):
        app.logger.info("Email not sent: SMTP settings missing. To=%s Subject=%s", to_email, subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception:
        app.logger.exception("Email sending failed")
        return False

def parse_times(value):
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise ValueError("Enter at least one time, for example 08:00, 20:00.")
    valid = []
    for p in parts:
        try:
            datetime.strptime(p, "%H:%M")
        except ValueError:
            raise ValueError(f"'{p}' is not a valid 24-hour time. Use HH:MM.")
        if p not in valid:
            valid.append(p)
    return sorted(valid)

def create_due_events():
    now = datetime.now()
    day = now.date().isoformat()
    with db() as c:
        meds = c.execute("""
          SELECT m.id,m.user_id,m.name,m.times FROM medications m
          WHERE m.active=1
        """).fetchall()
        for med in meds:
            for t in med["times"].split(","):
                t = t.strip()
                try:
                    scheduled = datetime.strptime(day + " " + t, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                # Materialize today's reminder events as soon as their scheduled time arrives.
                if scheduled <= now and scheduled >= now - timedelta(hours=24):
                    c.execute("""INSERT OR IGNORE INTO reminder_events
                      (user_id,medication_id,medication_name,scheduled_for,status)
                      VALUES(?,?,?,?, 'Pending')""",
                      (med["user_id"], med["id"], med["name"], scheduled.isoformat(timespec="minutes")))
        # Mark overdue pending reminders as unacknowledged once caregiver threshold passes.
        rows = c.execute("""
          SELECT re.id,re.user_id,re.medication_name,re.scheduled_for,
                 cs.enabled,cs.consent,cs.name,cs.email,cs.after_minutes
          FROM reminder_events re
          LEFT JOIN caregiver_settings cs ON cs.user_id=re.user_id
          WHERE re.status='Pending'
        """).fetchall()
        for row in rows:
            try:
                scheduled = datetime.fromisoformat(row["scheduled_for"])
            except ValueError:
                continue
            threshold = row["after_minutes"] if row["after_minutes"] is not None else 30
            if now >= scheduled + timedelta(minutes=threshold):
                c.execute("UPDATE reminder_events SET status='Unacknowledged' WHERE id=? AND status='Pending'", (row["id"],))
                if row["enabled"] and row["consent"] and row["email"]:
                    # Mark event first so a scheduler restart won't repeatedly send alerts.
                    c.execute("UPDATE reminder_events SET status='Flagged' WHERE id=?", (row["id"],))
                    send_email(row["email"], "MediMate AI: reminder not acknowledged",
                               f"Hello {row['name'] or 'Caregiver'},\\n\\nThe reminder for {row['medication_name']} scheduled at {scheduled.strftime('%d %b %Y, %I:%M %p')} has not been acknowledged in MediMate AI. This does not prove that the medicine was missed or taken. Please check in with the patient if appropriate.\\n\\nThis is an automated reminder, not medical advice.")
        # Notify appointments once their reminder window (24 hours before) begins.
        appts = c.execute("""SELECT * FROM appointments WHERE notified=0 AND appt_date >= ?""", (day,)).fetchall()
        for a in appts:
            try:
                when = datetime.strptime(a["appt_date"] + " " + (a["appt_time"] or "09:00"), "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if now >= when - timedelta(hours=24) and now <= when:
                c.execute("UPDATE appointments SET notified=1 WHERE id=?", (a["id"],))
                # Email appointment reminder to the account email.
                u = c.execute("SELECT email,name FROM users WHERE id=?", (a["user_id"],)).fetchone()
                if u:
                    send_email(u["email"], "MediMate AI: upcoming appointment",
                               f"Hello {u['name']},\\n\\nReminder: {a['title']} is scheduled for {when.strftime('%A, %d %B %Y at %I:%M %p')}.\\nDoctor/clinic: {a['doctor'] or 'Not specified'}\\nLocation: {a['location'] or 'Not specified'}\\n\\nMediMate AI is a reminder tool, not medical advice.")

def scheduler_job():
    try:
        create_due_events()
    except Exception:
        app.logger.exception("Reminder scheduler failed")

if BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(scheduler_job, "interval", seconds=30, id="medimate_reminders", max_instances=1, coalesce=True)
    scheduler.start()
else:
    scheduler = None

@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("landing.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or len(password) < 8:
            flash("Enter your name and email. Password must be at least 8 characters.", "error")
            return render_template("auth.html", mode="register")
        try:
            with db() as c:
                cur = c.execute("INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)",
                                (name, email, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")))
                uid = cur.lastrowid
                c.execute("INSERT INTO caregiver_settings(user_id) VALUES(?)", (uid,))
            session.clear()
            session["user_id"] = uid
            flash("Welcome to MediMate AI. Your account is ready.", "success")
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            flash("An account with that email already exists. Please sign in.", "error")
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with db() as c:
            user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        flash("Email or password is incorrect.", "error")
    return render_template("auth.html", mode="login")

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    today = date.today().isoformat()
    with db() as c:
        meds = c.execute("SELECT * FROM medications WHERE user_id=? AND active=1 ORDER BY name", (user["id"],)).fetchall()
        appts = c.execute("SELECT * FROM appointments WHERE user_id=? AND appt_date>=? ORDER BY appt_date,appt_time LIMIT 5", (user["id"],today)).fetchall()
        events = c.execute("SELECT * FROM reminder_events WHERE user_id=? ORDER BY scheduled_for DESC LIMIT 30", (user["id"],)).fetchall()
    return render_template("dashboard.html", user=user, meds=meds, appts=appts, events=events, today=today)

@app.route("/medications", methods=["GET", "POST"])
@login_required
def medications():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action", "save")
        med_id = request.form.get("med_id", type=int)
        if action == "delete":
            with db() as c:
                c.execute("DELETE FROM medications WHERE id=? AND user_id=?", (med_id,user["id"]))
            flash("Medicine schedule deleted.", "success")
            return redirect(url_for("medications"))
        try:
            name = request.form.get("name","").strip()
            instructions = request.form.get("instructions","").strip()
            frequency = request.form.get("frequency","As prescribed").strip()
            times = ",".join(parse_times(request.form.get("times","")))
            notes = request.form.get("notes","").strip()
            if not name:
                raise ValueError("Medicine name is required.")
            with db() as c:
                if med_id:
                    c.execute("UPDATE medications SET name=?,instructions=?,frequency=?,times=?,notes=? WHERE id=? AND user_id=?",
                              (name,instructions,frequency,times,notes,med_id,user["id"]))
                else:
                    c.execute("INSERT INTO medications(user_id,name,instructions,frequency,times,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                              (user["id"],name,instructions,frequency,times,notes,datetime.now().isoformat(timespec="seconds")))
            flash("Medicine schedule saved.", "success")
            return redirect(url_for("medications"))
        except ValueError as e:
            flash(str(e), "error")
    with db() as c:
        meds = c.execute("SELECT * FROM medications WHERE user_id=? ORDER BY name", (user["id"],)).fetchall()
    return render_template("medications.html", user=user, meds=meds)

@app.route("/appointments", methods=["GET", "POST"])
@login_required
def appointments():
    user = current_user()
    if request.method == "POST":
        action=request.form.get("action","save")
        appt_id=request.form.get("appt_id",type=int)
        if action=="delete":
            with db() as c: c.execute("DELETE FROM appointments WHERE id=? AND user_id=?",(appt_id,user["id"]))
            flash("Appointment deleted.","success")
            return redirect(url_for("appointments"))
        title=request.form.get("title","").strip()
        appt_date=request.form.get("appt_date","").strip()
        appt_time=request.form.get("appt_time","").strip()
        doctor=request.form.get("doctor","").strip()
        location=request.form.get("location","").strip()
        notes=request.form.get("notes","").strip()
        if not title or not appt_date:
            flash("Appointment title and date are required.","error")
        else:
            try: datetime.strptime(appt_date,"%Y-%m-%d")
            except ValueError:
                flash("Enter a valid appointment date.","error")
                return redirect(url_for("appointments"))
            with db() as c:
                if appt_id:
                    c.execute("UPDATE appointments SET title=?,doctor=?,appt_date=?,appt_time=?,location=?,notes=?,notified=0 WHERE id=? AND user_id=?",
                              (title,doctor,appt_date,appt_time,location,notes,appt_id,user["id"]))
                else:
                    c.execute("INSERT INTO appointments(user_id,title,doctor,appt_date,appt_time,location,notes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                              (user["id"],title,doctor,appt_date,appt_time,location,notes,datetime.now().isoformat(timespec="seconds")))
            flash("Appointment saved. An email reminder is attempted 24 hours before the visit if SMTP is configured.","success")
            return redirect(url_for("appointments"))
    with db() as c:
        appts=c.execute("SELECT * FROM appointments WHERE user_id=? ORDER BY appt_date,appt_time",(user["id"],)).fetchall()
    return render_template("appointments.html", user=user, appts=appts, today=date.today().isoformat())

@app.route("/history")
@login_required
def history():
    user=current_user()
    with db() as c:
        events=c.execute("SELECT * FROM reminder_events WHERE user_id=? ORDER BY scheduled_for DESC LIMIT 300",(user["id"],)).fetchall()
    return render_template("history.html", user=user, events=events)

@app.route("/reminders/poll")
@login_required
def reminder_poll():
    user=current_user()
    scheduler_job()
    with db() as c:
        events=c.execute("""SELECT re.*,m.times FROM reminder_events re LEFT JOIN medications m ON m.id=re.medication_id
          WHERE re.user_id=? AND re.scheduled_for >= ? ORDER BY re.scheduled_for DESC LIMIT 50""",
          (user["id"],(datetime.now()-timedelta(days=1)).isoformat(timespec="minutes"))).fetchall()
    return jsonify([dict(e) for e in events])

@app.route("/reminders/<int:event_id>/<action>", methods=["POST"])
@login_required
def reminder_action(event_id, action):
    user=current_user()
    if action not in ("acknowledge","snooze"):
        return jsonify({"ok":False,"error":"Unsupported action"}),400
    with db() as c:
        event=c.execute("SELECT * FROM reminder_events WHERE id=? AND user_id=?",(event_id,user["id"])).fetchone()
        if not event: return jsonify({"ok":False,"error":"Reminder not found"}),404
        if action=="acknowledge":
            c.execute("UPDATE reminder_events SET status='Acknowledged',acknowledged_at=? WHERE id=?",
                      (datetime.now().isoformat(timespec="seconds"),event_id))
            status="Acknowledged"
        else:
            # A snooze is recorded, then this event is snoozed for ten minutes.
            c.execute("UPDATE reminder_events SET status='Snoozed' WHERE id=?",(event_id,))
            status="Snoozed"
    return jsonify({"ok":True,"status":status})

@app.route("/caregiver", methods=["GET","POST"])
@login_required
def caregiver():
    user=current_user()
    if request.method=="POST":
        enabled=1 if request.form.get("enabled")=="on" else 0
        consent=1 if request.form.get("consent")=="on" else 0
        name=request.form.get("name","").strip()
        email=request.form.get("email","").strip()
        try: after=max(5,min(1440,int(request.form.get("after_minutes","30"))))
        except ValueError: after=30
        if enabled and (not consent or not name or not email):
            flash("To enable caregiver alerts, enter a name and email and confirm consent.","error")
        else:
            with db() as c:
                c.execute("""INSERT INTO caregiver_settings(user_id,enabled,name,email,consent,after_minutes)
                VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled,name=excluded.name,email=excluded.email,consent=excluded.consent,after_minutes=excluded.after_minutes""",
                (user["id"],enabled,name,email,consent,after))
            flash("Caregiver settings saved. Emails require SMTP settings in the README.","success")
            return redirect(url_for("caregiver"))
    with db() as c:
        settings=c.execute("SELECT * FROM caregiver_settings WHERE user_id=?",(user["id"],)).fetchone()
    return render_template("caregiver.html",user=user,settings=settings)

@app.route("/ai-helper", methods=["GET","POST"])
@login_required
def ai_helper():
    answer=None
    if request.method=="POST":
        instructions=request.form.get("instructions","").strip()
        if not instructions:
            flash("Paste the instructions you want help understanding.","error")
        else:
            # AI is opt-in and requires an API key; no user data is sent anywhere unless enabled.
            api_key=os.environ.get("OPENAI_API_KEY")
            if not api_key:
                answer=("AI mode is not configured yet. You can still use this page to check a safe format: "
                        "keep the original clinician instructions unchanged, ask your pharmacist about unclear "
                        "abbreviations, and never change a dose based on this demo. To enable AI, set OPENAI_API_KEY "
                        "and install the optional openai package as described in README.")
            else:
                try:
                    from openai import OpenAI
                    client=OpenAI(api_key=api_key)
                    response=client.responses.create(
                        model=os.environ.get("OPENAI_MODEL","gpt-4.1-mini"),
                        instructions=("You are a cautious healthcare instruction simplifier inside a reminder app. "
                        "Only restate user-provided instructions in plain language. Never prescribe, diagnose, infer "
                        "missing dose/frequency, recommend changes, or resolve ambiguous clinical instructions. "
                        "If anything is unclear or safety-sensitive, say to ask the prescribing clinician/pharmacist. "
                        "Include a short disclaimer that the explanation is not medical advice."),
                        input=instructions
                    )
                    answer=response.output_text
                except Exception:
                    app.logger.exception("AI helper failed")
                    answer="The AI helper could not respond. Please try again later or ask your pharmacist/doctor to clarify the original instructions."
    return render_template("ai_helper.html",user=current_user(),answer=answer)

@app.route("/health")
def health():
    return jsonify({"status":"ok","scheduler_available":scheduler is not None})

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", user=current_user(), code=404, message="We couldn't find that page."),404

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG","0")=="1", host="127.0.0.1", port=int(os.environ.get("PORT","5000")))
