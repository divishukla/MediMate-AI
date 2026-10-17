# MediMate AI — Full-stack student prototype

This is a runnable Flask + SQLite web application with registration/login, medicine schedule management, appointment management, reminder event tracking, optional consent-based caregiver email alerts, and an optional AI instruction simplifier.

## Run locally (Windows)
1. Install Python 3.10 or newer.
2. Extract this ZIP.
3. Open the extracted folder in VS Code.
4. Open Terminal → New Terminal.
5. Create a virtual environment:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks activation, use Command Prompt:
   ```bat
   .venv\Scripts\activate.bat
   ```
6. Install packages:
   ```powershell
   pip install -r requirements.txt
   ```
7. Start the website:
   ```powershell
   python app.py
   ```
8. Open http://127.0.0.1:5000 in your browser and create an account.

## Features implemented
- User registration and login; passwords stored as password hashes.
- Per-user medicine schedules: add schedules, instructions, frequency, times, notes, and delete.
- Per-user appointments: save visit date/time, doctor, location, notes, and delete.
- Background scheduler (APScheduler) checks every 30 seconds while the Flask process is running.
- Reminder events are materialized once the entered schedule time arrives; users can review event history.
- Caregiver consent/settings and configurable unacknowledged threshold; caregiver email is attempted if SMTP is configured.
- Appointment email reminder is attempted within 24 hours before appointment if SMTP is configured.
- Optional AI instruction simplifier. Without an API key it remains safely unconfigured and explains how to enable it.
- Responsive interface and health endpoint at `/health`.

## Optional email configuration
Email notifications are disabled unless SMTP settings are provided. Set environment variables before starting the app. Example in PowerShell (replace with your provider's real SMTP settings):
```powershell
$env:SMTP_HOST="smtp.example.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="your-user"
$env:SMTP_PASSWORD="your-app-password"
$env:SMTP_FROM="medimate@example.com"
python app.py
```
Use an email provider's app password or transactional email credentials, never your regular account password. Do not commit secrets to GitHub.

## Optional AI helper
1. Install the optional SDK:
   ```powershell
   pip install openai
   ```
2. Set an API key in your environment (never paste it into source code):
   ```powershell
   $env:OPENAI_API_KEY="your-api-key"
   python app.py
   ```
3. Open the AI instruction helper from the left menu. Only paste instructions with personal identifiers removed.

## Important limitations / safety
- This is a student prototype, not a production healthcare system. Do not enter real sensitive patient data.
- Scheduled jobs run only while `python app.py` is running. The app must be deployed to an always-on server for reliable reminders.
- Email delivery needs correct SMTP credentials and should be tested. Without SMTP configuration, no emails are sent.
- Browser push notifications are not implemented; this version uses server-side email attempts plus an in-app history/dashboard.
- “Acknowledged” means the user clicked the button; it does not prove that medicine was taken. The reminder history is for organizing routine, not verifying adherence.
- The AI helper only activates with an API key and the optional SDK. It must not prescribe, diagnose, or recommend medication changes. Always ask a qualified clinician or pharmacist about ambiguous instructions.
- Before public deployment, add CSRF protection, rate limiting, HTTPS, secure production cookie settings, backup/retention policies, and security/privacy review. Do not use Flask's development server for production.

## Test checklist
- Register two accounts and verify each only sees their own records.
- Add a medicine with a near-future time; keep the server running and check Reminder history after that time.
- Acknowledge a reminder only after the reminder action UI is added or via a future extension; current build records pending/unacknowledged/flagged events and displays history.
- Configure SMTP test credentials and verify appointment/caregiver emails with a test inbox before any real use.
