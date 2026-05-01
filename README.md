# KIU Online Automated Financial Management System

**PROJECT TITLE:** Online Automated Financial Management System: A Case Study of Kampala International University

**Academic Note:** This system was developed as a final-year project submission for Kampala International University, implementing the methodology described in Chapter 3, the implementation evidence of Chapter 4, and the validation criteria of Chapter 5 of the accompanying project report.

---

## System Purpose

This system automates student financial management at KIU, replacing manual processes with a secure, role-based web application that handles:

- Student fee balance tracking in real time
- Mobile money payment simulation (MTN MoMo / Airtel Money)
- Bank payment verification by Finance Administrators
- QR-code-enabled Examination Card generation (only when fully cleared)
- Financial reporting with CSV export
- Two-Factor Authentication (2FA) for all logins
- Role-based access control (System Admin, Finance Admin, Student)
- Audit logging of all system actions

---

## Technologies Used

| Layer          | Technology                        |
|----------------|-----------------------------------|
| Backend        | Python Flask 2.3                  |
| Database       | MySQL 8.0 (SQLite fallback)       |
| ORM            | SQLAlchemy + Flask-Migrate        |
| Frontend       | HTML5, CSS3, JavaScript, Jinja2   |
| Security       | Flask-WTF CSRF, Flask-Bcrypt, 2FA |
| QR Codes       | qrcode[pil] library               |
| Containerization | Docker + Docker Compose          |
| Web Server     | Gunicorn (production)             |

---

## Docker Setup (Recommended)

### Prerequisites
- Docker Desktop installed and running
- Git (optional)

### Quick Start

```bash
# 1. Clone / navigate to project directory
cd akello

# 2. Copy environment file
copy .env.example .env

# 3. Build and start all services
docker compose up --build

# 4. Access the system
#    App:        http://localhost:4005
#    phpMyAdmin: http://localhost:8080
```

### Stop the system
```bash
docker compose down
```

### Reset database (fresh start)
```bash
docker compose down -v
docker compose up --build
```

---

## Default Login Accounts

> **All accounts require 2FA OTP verification after password entry.**
> OTP codes print to the Docker logs console (Simulation Mode for local demo).

| Role             | Email                              | Password     | Notes                      |
|------------------|------------------------------------|--------------|----------------------------|
| System Admin     | admin@kiu.ac.ug                    | admin123     | Full system access         |
| Finance Admin    | finance@kiu.ac.ug                  | finance123   | Financial management       |
| Student (Cleared)| alice.nakato@student.kiu.ac.ug     | student123   | 100% paid — can get exam card |
| Student (Partial)| bob.mwangi@student.kiu.ac.ug       | student123   | 50% paid                   |
| Student (Pending)| carol.auma@student.kiu.ac.ug       | student123   | No payment yet             |

### How to get the OTP during demo
```bash
# View Docker logs in real time
docker compose logs -f app
# Look for line: [2FA LOGIN OTP] User: admin@kiu.ac.ug  OTP: 123456
```

---

## Folder Structure

```
akello/
├── app.py                    # Main Flask application
├── seed.py                   # Database seed script
├── requirements.txt          # Python dependencies
├── config.py                 # Configuration classes
├── Dockerfile                # Flask app container
├── docker-compose.yml        # Multi-service setup
├── docker-entrypoint.sh      # Startup script
├── .env                      # Environment variables (not committed)
├── .env.example              # Template for .env
├── README.md                 # This file
├── templates/                # Jinja2 HTML templates (50+)
│   ├── login.html
│   ├── verify_2fa.html       # 2FA OTP page
│   ├── verify_card.html      # Public QR verification
│   ├── student_dashboard.html
│   ├── finace_staff_dashboard.html
│   ├── admin_dashboard.html
│   ├── report_summary.html
│   ├── report_outstanding.html
│   ├── report_cleared.html
│   ├── report_payment_history.html
│   ├── report_failed.html
│   ├── notifications.html
│   └── ...
└── static/
    ├── qrcodes/              # Generated QR code PNGs
    ├── css/
    └── js/
```

---

## How Payment Simulation Works

This system uses **Simulation Mode** for mobile money payments (MTN MoMo / Airtel Money), clearly marked in the UI. No external payment API is required for local testing.

### Student Flow:
1. Student logs in and navigates to **Make Payment**
2. Selects payment method: MTN MoMo, Airtel Money, or Bank Transfer
3. Enters amount and phone number
4. For mobile money: a **Pending** transaction is created instantly
5. For Bank Transfer: student uploads proof, Finance Admin verifies manually

### Finance Admin Simulation Callback:
1. Finance Admin opens **Payment Verification** dashboard
2. Sees all pending transactions with student details
3. Clicks **[SIMULATION] Mark as Completed** or **Mark as Failed**
4. System updates student balance, creates notification, logs audit entry

### Automatic (Mobile Money Demo):
- Mobile money payments also have an **auto-confirm** mode that immediately marks the transaction as Confirmed for quick demo purposes

---

## How to Test QR Examination Card Verification

1. Login as **Alice Nakato** (alice.nakato@student.kiu.ac.ug / student123)
2. Complete 2FA with OTP from Docker logs
3. Go to **Examination Card** — card generates automatically (fully cleared)
4. Note the **Card Number** (format: KIU-EC-XXXXXXXX)
5. Open in browser: `http://localhost:4005/verify-card/KIU-EC-XXXXXXXX`
6. System shows: ✅ CARD VALID with full student details

### Finance Admin can revoke a card:
- Go to **Payment Verification** → find exam card → click **Revoke**
- Re-scan QR: shows ❌ CARD INVALID — revoked

---

## Report URLs

| Report                   | URL                                   |
|--------------------------|---------------------------------------|
| Financial Summary        | /reports/summary                      |
| Outstanding Balances     | /reports/outstanding                  |
| Cleared Students         | /reports/cleared                      |
| Payment History          | /reports/payment-history              |
| Failed Payments          | /reports/failed                       |
| CSV Export (any report)  | /reports/export/<report-type>         |

---

## Testing Checklist

- [ ] Login with correct credentials → 2FA OTP page appears
- [ ] Enter correct OTP → redirected to role dashboard
- [ ] Enter wrong OTP → error shown, not logged in
- [ ] Student cannot access /admin/ or /finance/ routes (403 redirect)
- [ ] Finance admin cannot access /admin/ routes
- [ ] Student with outstanding balance cannot download exam card
- [ ] Alice (cleared) can generate and download exam card with QR
- [ ] QR URL `/verify-card/<card_number>` shows VALID for active card
- [ ] Finance admin revokes card → QR shows INVALID
- [ ] Payment exceeding balance → validation error shown
- [ ] Finance admin simulation callback marks payment confirmed
- [ ] Student balance updates after confirmed payment
- [ ] Notifications appear after payment confirmation
- [ ] All 6 report pages load with real data
- [ ] CSV export downloads correctly
- [ ] Audit logs show login, payment, exam card, revocation events
- [ ] System admin can create, disable, and change roles of users

---

## Security Features Implemented

- ✅ Two-Factor Authentication (2FA) on all logins
- ✅ CSRF protection via Flask-WTF on all forms
- ✅ SQL injection prevention via SQLAlchemy ORM
- ✅ Password hashing via Flask-Bcrypt (bcrypt)
- ✅ Role-based route protection decorators
- ✅ Session timeout (30 minutes)
- ✅ XSS-safe Jinja2 auto-escaping
- ✅ Audit logging for all sensitive actions
- ✅ No plaintext passwords stored or displayed
- ✅ Environment variables for all secrets (no hardcoded credentials)

---

*Kampala International University — Final Year Project 2025/2026*
