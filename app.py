import os
import random
import string
import json
import csv
import uuid
from datetime import datetime, timedelta
from functools import wraps
from io import StringIO

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect, generate_csrf
from werkzeug.utils import secure_filename
import requests

# ------------------------------
# App configuration
# ------------------------------
app = Flask(__name__)

_mysql_user = os.environ.get('MYSQL_USER')
_mysql_pass = os.environ.get('MYSQL_PASSWORD')
_mysql_host = os.environ.get('MYSQL_HOST', 'localhost')
_mysql_port = os.environ.get('MYSQL_PORT', '3306')
_mysql_db   = os.environ.get('MYSQL_DB', 'kiu_finance')

if _mysql_user and _mysql_pass:
    _db_uri = f'mysql+pymysql://{_mysql_user}:{_mysql_pass}@{_mysql_host}:{_mysql_port}/{_mysql_db}'
else:
    _db_uri = 'sqlite:///portal.db'
    print('WARNING: MySQL not configured – using SQLite for local dev.')

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'kiu-dev-secret-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = _db_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 300}
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['PAYMENT_PROOF_UPLOAD_FOLDER'] = os.path.join(app.instance_path, 'payment_proofs')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
app.config['WTF_CSRF_TIME_LIMIT'] = 3600

app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']        = True
app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@kiu.ac.ug')

APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://localhost:5000')

# ------------------------------
# Extensions
# ------------------------------
db            = SQLAlchemy(app)
bcrypt        = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view    = 'login'
login_manager.login_message = 'Please log in to access this page.'
mail    = Mail(app)
csrf    = CSRFProtect(app)
migrate = Migrate(app, db)

# ------------------------------
# Helper functions
# ------------------------------
def generate_otp():
    return ''.join(random.choices(string.digits, k=6))

def generate_transaction_id():
    return f"TRX-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(1000,9999)}"

def generate_registration_number():
    year = datetime.now().year
    sequence = random.randint(10000, 99999)
    return f"KIU/{year}/{sequence}"

def send_otp_email(user_email, otp, purpose='reset'):
    subject = f'Your {purpose} OTP for KIU Financial Portal'
    body = f'Your OTP code is: {otp}. It will expire in 10 minutes.'
    try:
        msg = Message(subject, recipients=[user_email], body=body)
        mail.send(msg)
        print(f"OTP sent to {user_email}: {otp}")
        return True
    except Exception as e:
        print(f"[EMAIL FALLBACK] OTP for {user_email}: {otp}  (reason: {e})")
        return False


def generate_qr_code(data, filename):
    """Generate QR code PNG. Returns relative static path or None."""
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color='#0d3b6e', back_color='white')
        qr_dir = os.path.join(app.root_path, 'static', 'qrcodes')
        os.makedirs(qr_dir, exist_ok=True)
        img.save(os.path.join(qr_dir, filename))
        return f'qrcodes/{filename}'
    except Exception as e:
        print(f'QR generation failed: {e}')
        return None


def notify(user_id, message, ntype='system'):
    """Create a system notification for a user."""
    db.session.add(Notification(user_id=user_id, message=message, type=ntype))

def get_fee_threshold_status(user):
    ledger = user.ledger
    if not ledger or not ledger.total_billed:
        return {'exam_eligible': False, 'reg_eligible': False, 'percent_paid': 0}
    percent_paid = (float(ledger.total_paid) / float(ledger.total_billed)) * 100
    return {
        'exam_eligible': percent_paid >= EXAM_CARD_THRESHOLD,
        'reg_eligible': percent_paid >= SEMESTER_REGISTRATION_THRESHOLD,
        'percent_paid': round(percent_paid, 2)
    }

STUDENT_FEE_BREAKDOWN = {
    'tuition': 2500000,
    'functional_fees': 650000,
    'exam_fee': 50000,
}
STUDENT_TOTAL_DUES = sum(STUDENT_FEE_BREAKDOWN.values())
SEMESTER_REGISTRATION_THRESHOLD = 30
EXAM_CARD_THRESHOLD = 100
PREDEFINED_COURSES = [
    ('BIT 2201', 'Database Systems'),
    ('BCS 2205', 'Software Engineering'),
    ('BIT 2208', 'Web Application Development'),
    ('GST 2201', 'Communication Skills'),
    ('BBA 2104', 'Business Finance'),
    ('LAW 2301', 'Commercial Law'),
]

def finance_summary():
    total_students = User.query.filter_by(role='student', is_active=True).count()
    total_revenue = db.session.query(db.func.sum(Transaction.amount)).filter(Transaction.status == 'Confirmed').scalar() or 0
    total_outstanding = db.session.query(db.func.sum(FeeLedger.outstanding)).scalar() or 0
    pending_verifications = Transaction.query.filter_by(status='Pending').count()
    latest_transaction = Transaction.query.order_by(Transaction.created_at.desc()).first()
    outstanding_students = db.session.query(User, FeeLedger).join(FeeLedger, User.id == FeeLedger.user_id).filter(
        User.role == 'student',
        FeeLedger.outstanding > 0
    ).order_by(FeeLedger.outstanding.desc()).all()
    return {
        'total_students': total_students,
        'total_revenue': total_revenue,
        'total_collected': total_revenue,
        'total_outstanding': total_outstanding,
        'pending_verifications': pending_verifications,
        'latest_transaction_id': latest_transaction.id if latest_transaction else 0,
        'latest_transaction_at': latest_transaction.created_at.isoformat() if latest_transaction and latest_transaction.created_at else None,
        'outstanding_students': outstanding_students,
    }

def find_student(identifier):
    if not identifier:
        return None
    return User.query.join(StudentProfile).filter(
        (User.username == identifier) |
        (User.email == identifier) |
        (StudentProfile.reg_number == identifier) |
        (StudentProfile.full_name.ilike(f'%{identifier}%'))
    ).first()

def post_payment(user, amount, method, description, status='Confirmed', reference=None, detail=None):
    transaction = Transaction(
        user_id=user.id,
        transaction_id=reference or generate_transaction_id(),
        amount=amount,
        method=method,
        description=description,
        status=status,
        payment_method_detail=detail,
        confirmed_at=datetime.utcnow() if status == 'Confirmed' else None
    )
    if status == 'Confirmed' and user.ledger:
        user.ledger.total_paid += amount
        user.ledger.update_balance()
        user.ledger.last_updated = datetime.utcnow()
        # Notify student of confirmed payment
        notify(user.id, f'Payment of UGX {amount:,.0f} via {method} confirmed. Ref: {transaction.transaction_id}')
    db.session.add(transaction)
    return transaction

def simulate_mobile_money_payment(user, amount, provider, phone_number):
    if not user.ledger:
        user.ledger = FeeLedger(user_id=user.id, total_billed=STUDENT_TOTAL_DUES, total_paid=0, outstanding=STUDENT_TOTAL_DUES)
        db.session.add(user.ledger)

    outstanding = max(float(user.ledger.outstanding or 0), 0)
    if outstanding <= 0:
        raise ValueError('Your fees are already fully paid.')
    if amount <= 0:
        raise ValueError('Payment amount must be greater than zero.')
    if amount > outstanding:
        amount = outstanding

    provider_label = 'MTN MoMo' if provider == 'mtn' else 'Airtel Money'
    reference = f"SIM-{provider.upper()}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(100, 999)}"
    return post_payment(
        user,
        amount,
        provider_label,
        f'Demo {provider_label} payment of UGX {amount:,.2f}',
        status='Confirmed',
        reference=reference,
        detail=phone_number
    )

def normalize_payment_amount(user, amount):
    if not user.ledger:
        user.ledger = FeeLedger(user_id=user.id, total_billed=STUDENT_TOTAL_DUES, total_paid=0, outstanding=STUDENT_TOTAL_DUES)
        db.session.add(user.ledger)

    outstanding = max(float(user.ledger.outstanding or 0), 0)
    if outstanding <= 0:
        raise ValueError('Your fees are already fully paid.')
    if amount <= 0:
        raise ValueError('Payment amount must be greater than zero.')
    return min(amount, outstanding)

def save_payment_proof(file_storage):
    if not file_storage or not file_storage.filename:
        return None

    filename = secure_filename(file_storage.filename)
    if not filename:
        return None

    os.makedirs(app.config['PAYMENT_PROOF_UPLOAD_FOLDER'], exist_ok=True)
    unique_filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}-{filename}"
    file_storage.save(os.path.join(app.config['PAYMENT_PROOF_UPLOAD_FOLDER'], unique_filename))
    return unique_filename

# ------------------------------
# DECORATORS
# ------------------------------
def role_required(*roles):
    """Decorator to restrict access to the requested user roles."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('Please log in to access this page.', 'warning')
                return redirect(url_for('login'))
            if current_user.role not in roles:
                flash('Access denied for your role.', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def admin_required(f):
    """Decorator to restrict access to admin users only"""
    return role_required('admin')(f)

def finance_staff_required(f):
    """Decorator to restrict access to finance staff only"""
    return role_required('finance_staff', 'admin')(f)

# ------------------------------
# Database Models
# ------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id              = db.Column(db.Integer, primary_key=True)
    username        = db.Column(db.String(50), unique=True, nullable=False)
    email           = db.Column(db.String(120), unique=True, nullable=False)
    password_hash   = db.Column(db.String(256), nullable=False)
    phone           = db.Column(db.String(20))
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    is_admin        = db.Column(db.Boolean, default=False)
    is_finance_staff = db.Column(db.Boolean, default=False)
    role            = db.Column(db.String(50), default='student')
    is_active       = db.Column(db.Boolean, default=True, nullable=False)
    # 2FA fields
    two_factor_enabled = db.Column(db.Boolean, default=True)
    otp_code           = db.Column(db.String(6))
    otp_expires_at     = db.Column(db.DateTime)

    profile       = db.relationship('StudentProfile', backref='user', uselist=False, cascade='all, delete-orphan')
    ledger        = db.relationship('FeeLedger', backref='user', uselist=False, cascade='all, delete-orphan')
    transactions  = db.relationship('Transaction', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    exam_cards    = db.relationship('ExamCard', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    registrations = db.relationship('Registration', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='user', lazy='dynamic', cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

class StudentProfile(db.Model):
    __tablename__ = 'student_profiles'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    reg_number = db.Column(db.String(50), unique=True, nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    course = db.Column(db.String(100), nullable=False)
    year_of_study = db.Column(db.Integer, default=1)
    semester = db.Column(db.String(20), default='Semester I')
    campus = db.Column(db.String(50), default='Main Campus')
    phone_primary = db.Column(db.String(20))
    emergency_contact = db.Column(db.String(100))
    address = db.Column(db.String(200))

class FeeLedger(db.Model):
    __tablename__ = 'fee_ledgers'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    total_billed = db.Column(db.Float, default=3200000.00)
    total_paid   = db.Column(db.Float, default=0.0)
    outstanding  = db.Column(db.Float, default=3200000.00)
    status       = db.Column(db.String(20), default='pending')  # pending, partial, cleared
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

    def update_balance(self):
        self.outstanding = max(0.0, self.total_billed - self.total_paid)
        if self.outstanding <= 0:
            self.status = 'cleared'
        elif self.total_paid > 0:
            self.status = 'partial'
        else:
            self.status = 'pending'

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    transaction_id = db.Column(db.String(50), unique=True, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(50))
    description = db.Column(db.String(200))
    status = db.Column(db.String(20), default='Pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    mtn_reference_id = db.Column(db.String(100))
    mtn_status = db.Column(db.String(50))
    payment_method_detail = db.Column(db.String(50))

class ExamCard(db.Model):
    __tablename__ = 'exam_cards'
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    semester      = db.Column(db.String(20))
    academic_year = db.Column(db.String(20))
    generated_at  = db.Column(db.DateTime, default=datetime.utcnow)
    qr_code_data  = db.Column(db.String(500))
    card_number   = db.Column(db.String(30), unique=True)
    qr_code_path  = db.Column(db.String(300))
    status        = db.Column(db.String(20), default='generated')  # generated, revoked


class Notification(db.Model):
    __tablename__ = 'notifications'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    message    = db.Column(db.String(500), nullable=False)
    type       = db.Column(db.String(20), default='system')  # sms, email, system
    status     = db.Column(db.String(20), default='unread')  # unread, read
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Registration(db.Model):
    __tablename__ = 'registrations'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    semester = db.Column(db.String(20))
    academic_year = db.Column(db.String(20))
    courses_registered = db.Column(db.Text)
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_confirmed = db.Column(db.Boolean, default=True)

class PasswordResetOTP(db.Model):
    __tablename__ = 'password_reset_otp'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    otp_code = db.Column(db.String(6), nullable=False)
    purpose = db.Column(db.String(20), default='reset')
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)

class MTNCredential(db.Model):
    __tablename__ = 'mtn_credentials'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    api_user_id = db.Column(db.String(200))
    api_key = db.Column(db.String(200))
    subscription_key = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = db.relationship('User', backref='mtn_credentials', uselist=False)

class AirtelCredential(db.Model):
    __tablename__ = 'airtel_credentials'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    client_id = db.Column(db.String(200))
    client_secret = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref='audit_logs')

class FinancialReport(db.Model):
    __tablename__ = 'financial_reports'
    id = db.Column(db.Integer, primary_key=True)
    report_type = db.Column(db.String(50))
    title = db.Column(db.String(200))
    report_data = db.Column(db.Text)
    generated_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    date_range_start = db.Column(db.DateTime)
    date_range_end = db.Column(db.DateTime)
    
    generator = db.relationship('User', backref='generated_reports')

class FeeStructure(db.Model):
    __tablename__ = 'fee_structures'
    id = db.Column(db.Integer, primary_key=True)
    academic_year = db.Column(db.String(20), nullable=False)
    semester = db.Column(db.String(20), nullable=False)
    programme = db.Column(db.String(100))
    amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.DateTime)
    late_penalty = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ------------------------------
# Flask-Login loader
# ------------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ------------------------------
# Context processor for template year
# ------------------------------
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

def ensure_default_users():
    """Create the demo accounts printed at startup if they do not exist."""
    defaults = [
        ('admin', 'admin@kiu.ac.ug', 'admin123', True, False, 'admin', 'System Administrator'),
        ('finance', 'finance@kiu.ac.ug', 'finance123', False, True, 'finance_staff', 'Finance Staff'),
        ('student', 'student@kiu.ac.ug', 'student123', False, False, 'student', 'Demo Student'),
    ]

    for username, email, password, is_admin, is_finance_staff, role, full_name in defaults:
        user = User.query.filter_by(username=username).first()
        if not user:
            user = User(
                username=username,
                email=email,
                is_admin=is_admin,
                is_finance_staff=is_finance_staff,
                role=role,
                is_active=True
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
        else:
            user.is_admin = is_admin
            user.is_finance_staff = is_finance_staff
            user.role = role
            if user.is_active is None:
                user.is_active = True
            user.set_password(password)

        if not user.profile:
            db.session.add(StudentProfile(
                user_id=user.id,
                reg_number=generate_registration_number(),
                full_name=full_name,
                course='Bachelor of Information Technology',
                year_of_study=1
            ))
        if not user.ledger:
            db.session.add(FeeLedger(
                user_id=user.id,
                total_billed=3200000.00,
                total_paid=0,
                outstanding=3200000.00
            ))

    User.query.filter(User.is_active.is_(None)).update({User.is_active: True}, synchronize_session=False)
    db.session.commit()

# ------------------------------
# Authentication Routes
# ------------------------------
@app.route('/')
def home():
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm = request.form.get('confirm_password')
        full_name = request.form.get('full_name')
        phone = request.form.get('phone')
        
        if password != confirm:
            flash('Passwords do not match', 'danger')
            return render_template('register.html')
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash('Username or email already exists', 'danger')
            return render_template('register.html')
            
        user = User(username=username, email=email, phone=phone, is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        
        profile = StudentProfile(
            user_id=user.id,
            reg_number=generate_registration_number(),
            full_name=full_name or username.title(),
            course='Bachelor of Information Technology',
            year_of_study=1,
            phone_primary=phone
        )
        ledger = FeeLedger(
            user_id=user.id,
            total_billed=3200000.00,
            total_paid=0,
            outstanding=3200000.00
        )
        db.session.add_all([profile, ledger])
        db.session.commit()
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
@csrf.exempt
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        identifier = request.form.get('id')
        password   = request.form.get('password')
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()
        if user and user.check_password(password):
            if not user.is_active:
                flash('Account deactivated. Contact administrator.', 'danger')
                return render_template('login.html')
            # --- 2FA: generate OTP and redirect to verification ---
            otp = generate_otp()
            user.otp_code       = otp
            user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
            db.session.commit()
            session['_2fa_uid'] = user.id
            
            # Send real email
            send_otp_email(user.email, otp, purpose='login verification')
            
            flash('An OTP has been sent to your registered email.', 'info')
            return redirect(url_for('verify_2fa'))
        else:
            flash('Invalid credentials', 'danger')
    return render_template('login.html')


@app.route('/2fa', methods=['GET', 'POST'])
@csrf.exempt
def verify_2fa():
    uid = session.get('_2fa_uid')
    if not uid:
        return redirect(url_for('login'))
    user = User.query.get(uid)
    if not user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        entered = ''.join([
            request.form.get(f'otp{i}', '') for i in range(1, 7)
        ]).strip() or request.form.get('otp_code', '').strip()
        if (user.otp_code and user.otp_expires_at
                and datetime.utcnow() < user.otp_expires_at
                and user.otp_code == entered):
            user.otp_code       = None
            user.otp_expires_at = None
            db.session.commit()
            session.pop('_2fa_uid', None)
            login_user(user)
            db.session.add(AuditLog(
                user_id=user.id, action='LOGIN',
                details='Logged in via 2FA OTP',
                ip_address=request.remote_addr
            ))
            db.session.commit()
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid or expired OTP. Please try again.', 'danger')
    return render_template('verify_2fa.html', email=user.email)

@app.route('/logout')
@login_required
def logout():
    db.session.add(AuditLog(user_id=current_user.id, action='LOGOUT', details='User logged out', ip_address=request.remote_addr))
    db.session.commit()
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# ------------------------------
# Password Reset Flow
# ------------------------------
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        identifier = request.form.get('id-number')
        user = User.query.filter((User.username == identifier) | (User.email == identifier)).first()
        if not user:
            flash('User not found', 'danger')
            return render_template('forgot_password.html')
        otp = generate_otp()
        expire_time = datetime.utcnow() + timedelta(minutes=10)
        PasswordResetOTP.query.filter_by(user_id=user.id, used=False).delete()
        db.session.commit()
        otp_record = PasswordResetOTP(user_id=user.id, otp_code=otp, expires_at=expire_time, purpose='reset')
        db.session.add(otp_record)
        db.session.commit()
        send_otp_email(user.email, otp, 'password reset')
        session['reset_user_id'] = user.id
        flash('OTP sent to your registered email.', 'info')
        return redirect(url_for('otp_verification', purpose='reset'))
    return render_template('forgot_password.html')

@app.route('/otp', methods=['GET', 'POST'])
def otp_verification():
    purpose = request.args.get('purpose', 'reset')
    if request.method == 'POST':
        otp_input = ''.join([request.form.get(f'otp{i}', '') for i in range(1,7)])
        user_id = session.get('reset_user_id') or session.get('temp_user_id')
        if not user_id:
            flash('Session expired. Please restart.', 'danger')
            return redirect(url_for('login'))
        otp_record = PasswordResetOTP.query.filter_by(user_id=user_id, otp_code=otp_input, used=False).first()
        if otp_record and otp_record.expires_at > datetime.utcnow():
            otp_record.used = True
            db.session.commit()
            if purpose == 'reset':
                return redirect(url_for('reset_password'))
            else:
                user = User.query.get(user_id)
                login_user(user)
                session.pop('temp_user_id', None)
                flash('OTP verified. Logged in.', 'success')
                return redirect(url_for('dashboard'))
        else:
            flash('Invalid or expired OTP', 'danger')
    return render_template('otp_verification.html')

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    user_id = session.get('reset_user_id')
    if not user_id:
        flash('Unauthorized', 'danger')
        return redirect(url_for('login'))
    user = User.query.get(user_id)
    if request.method == 'POST':
        new_password = request.form.get('new-password')
        confirm = request.form.get('confirm-password')
        if new_password != confirm:
            flash('Passwords do not match', 'danger')
            return render_template('password_reset.html')
        user.set_password(new_password)
        db.session.commit()
        session.pop('reset_user_id', None)
        flash('Password reset successful. Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('password_reset.html')

# ------------------------------
# Core Dashboard (Role-Based)
# ------------------------------
@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    elif current_user.role == 'finance_staff':
        return redirect(url_for('finance_dashboard'))
    else:
        if not current_user.ledger:
            ledger = FeeLedger(user_id=current_user.id, total_billed=3200000.00, total_paid=0, outstanding=3200000.00)
            db.session.add(ledger)
            db.session.commit()
        else:
            ledger = current_user.ledger
        recent_txs = current_user.transactions.order_by(Transaction.created_at.desc()).limit(3).all()
        threshold = get_fee_threshold_status(current_user)
        return render_template('student_dashboard.html',
                               user=current_user,
                               ledger=ledger,
                               recent_transactions=recent_txs,
                               exam_eligible=threshold['exam_eligible'],
                               reg_eligible=threshold['reg_eligible'],
                               percent_paid=threshold['percent_paid'],
                               fee_breakdown=STUDENT_FEE_BREAKDOWN,
                               total_dues=STUDENT_TOTAL_DUES)

# ------------------------------
# ADMIN MODULE ROUTES (FIXED AND WORKING)
# ------------------------------

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    """Admin Dashboard with system statistics"""
    summary = finance_summary()
    total_users = User.query.count()
    total_students = User.query.filter_by(role='student', is_admin=False, is_finance_staff=False).count()
    total_finance_staff = User.query.filter_by(is_finance_staff=True, is_admin=False).count()
    total_admins = User.query.filter_by(is_admin=True).count()
    recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()
    
    stats = {
        'total_users': total_users,
        'total_students': total_students,
        'total_finance_staff': total_finance_staff,
        'total_admins': total_admins,
        'total_revenue': summary['total_revenue'],
        'total_outstanding': summary['total_outstanding'],
        'pending_verifications': summary['pending_verifications']
    }
    
    return render_template('admin_dashboard.html', 
                          user=current_user, 
                          stats=stats,
                          recent_users=recent_users)

@app.route('/admin/user-management')
@admin_required
def user_management():
    """User Management - View all users"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    role_filter = request.args.get('role', 'all')
    status_filter = request.args.get('status', 'all')
    
    query = User.query
    
    if role_filter == 'admin':
        query = query.filter_by(is_admin=True)
    elif role_filter == 'finance':
        query = query.filter_by(is_finance_staff=True)
    elif role_filter == 'student':
        query = query.filter_by(role='student', is_admin=False, is_finance_staff=False)
    
    if status_filter == 'active':
        query = query.filter_by(is_active=True)
    elif status_filter == 'inactive':
        query = query.filter_by(is_active=False)
    
    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    return render_template('user_management.html',
                          user=current_user,
                          users=pagination.items,
                          pagination=pagination,
                          role_filter=role_filter,
                          status_filter=status_filter)

@app.route('/admin/users/create', methods=['POST'])
@admin_required
def create_user_admin():
    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or 'ChangeMe123'
    role = request.form.get('role') or 'student'
    full_name = (request.form.get('full_name') or username).strip()

    if not username or not email:
        flash('Username and email are required.', 'danger')
        return redirect(url_for('user_management'))
    if User.query.filter((User.username == username) | (User.email == email)).first():
        flash('Username or email already exists.', 'danger')
        return redirect(url_for('user_management'))

    user = User(username=username, email=email, role='student', is_active=True)
    user.set_password(password)
    if role == 'admin':
        user.role = 'admin'
        user.is_admin = True
    elif role == 'finance_staff':
        user.role = 'finance_staff'
        user.is_finance_staff = True

    db.session.add(user)
    db.session.flush()
    if user.role == 'student':
        db.session.add(StudentProfile(
            user_id=user.id,
            reg_number=generate_registration_number(),
            full_name=full_name,
            course=request.form.get('course') or 'Bachelor of Information Technology',
            year_of_study=1
        ))
        db.session.add(FeeLedger(user_id=user.id, total_billed=STUDENT_TOTAL_DUES, total_paid=0, outstanding=STUDENT_TOTAL_DUES))
    db.session.add(AuditLog(user_id=current_user.id, action='USER_CREATED',
                            details=f'Created {username} as {user.role}', ip_address=request.remote_addr))
    db.session.commit()
    flash(f'User {username} created. Temporary password: {password}', 'success')
    return redirect(url_for('user_management'))

@app.route('/admin/role-management')
@admin_required
def role_management():
    """Role Management - Configure user roles and permissions"""
    admins = User.query.filter_by(is_admin=True).all()
    finance_staff = User.query.filter_by(is_finance_staff=True, is_admin=False).all()
    students = User.query.filter_by(role='student', is_admin=False, is_finance_staff=False).all()
    
    return render_template('role_management.html',
                          user=current_user,
                          admins=admins,
                          finance_staff=finance_staff,
                          students=students)

@app.route('/admin/toggle-user-status/<int:user_id>')
@admin_required
def toggle_user_status(user_id):
    """Activate or deactivate a user account"""
    target_user = User.query.get_or_404(user_id)
    
    if target_user.id == current_user.id:
        flash('You cannot deactivate your own account!', 'danger')
        return redirect(url_for('user_management'))
    
    target_user.is_active = not target_user.is_active
    db.session.commit()
    
    status = "activated" if target_user.is_active else "deactivated"
    flash(f'User {target_user.username} has been {status}.', 'success')
    return redirect(url_for('user_management'))

@app.route('/admin/change-user-role/<int:user_id>', methods=['POST'])
@admin_required
def change_user_role(user_id):
    """Change a user's role"""
    target_user = User.query.get_or_404(user_id)
    new_role = request.form.get('role')
    
    if target_user.id == current_user.id:
        flash('You cannot change your own role!', 'danger')
        return redirect(url_for('user_management'))
    
    target_user.is_admin = False
    target_user.is_finance_staff = False
    
    if new_role == 'admin':
        target_user.is_admin = True
        target_user.role = 'admin'
    elif new_role == 'finance':
        target_user.is_finance_staff = True
        target_user.role = 'finance_staff'
    else:
        target_user.role = 'student'
    
    db.session.commit()
    flash(f'Role for {target_user.username} has been updated to {new_role}.', 'success')
    return redirect(url_for('user_management'))

@app.route('/admin/finance-oversight')
@admin_required
def finance_admin_dashboard():
    """Finance Admin Dashboard - Financial oversight"""
    summary = finance_summary()
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()
    
    return render_template('finance_admin_dashboard.html',
                          user=current_user,
                          total_collected=summary['total_collected'],
                          total_outstanding=summary['total_outstanding'],
                          pending_transactions=summary['pending_verifications'],
                          recent_transactions=recent_transactions,
                          outstanding_students=summary['outstanding_students'])

@app.route('/admin/finance-reports')
@admin_required
def finance_admin_reports():
    """Financial Reports for Admin"""
    return render_template('finance_admin_reports.html', user=current_user)

@app.route('/admin/system-configuration')
@admin_required
def system_configuration():
    """System Configuration"""
    return render_template('system_configuration.html', user=current_user)

@app.route('/admin/manage-fee-structure', methods=['GET', 'POST'])
@admin_required
def manage_fee_structure():
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount') or 0)
            late_penalty = float(request.form.get('late_penalty') or 0)
        except ValueError:
            flash('Amount and late penalty must be valid numbers.', 'danger')
            return redirect(url_for('manage_fee_structure'))

        due_date = None
        if request.form.get('due_date'):
            due_date = datetime.strptime(request.form.get('due_date'), '%Y-%m-%d')

        fee = FeeStructure(
            academic_year=request.form.get('academic_year'),
            semester=request.form.get('semester'),
            programme=request.form.get('programme') or 'All Programmes',
            amount=amount,
            due_date=due_date,
            late_penalty=late_penalty
        )
        db.session.add(fee)
        db.session.add(AuditLog(user_id=current_user.id, action='FEE_STRUCTURE_CREATED',
                                details=f'{fee.academic_year} {fee.semester} {fee.programme}', ip_address=request.remote_addr))
        db.session.commit()
        flash('Fee structure saved.', 'success')
        return redirect(url_for('manage_fee_structure'))

    fee_structures = FeeStructure.query.order_by(FeeStructure.created_at.desc()).all()
    return render_template('manage_fee_structure.html', user=current_user, fee_structures=fee_structures)

@app.route('/admin/audit-logs')
@admin_required
def audit_logs():
    """View system audit logs"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    pagination = AuditLog.query.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('audit_logs.html', user=current_user, logs=pagination.items, pagination=pagination)

# ------------------------------
# FINANCE STAFF MODULE ROUTES
# ------------------------------

@app.route('/finance/dashboard')
@finance_staff_required
def finance_dashboard():
    """Finance Staff Dashboard"""
    summary = finance_summary()
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()
    
    return render_template('finace_staff_dashboard.html',
                          user=current_user,
                          total_students=summary['total_students'],
                          total_collected=summary['total_collected'],
                          total_outstanding=summary['total_outstanding'],
                          pending_verifications=summary['pending_verifications'],
                          recent_transactions=recent_transactions,
                          outstanding_students=summary['outstanding_students'])

@app.route('/finance/student-registration')
@finance_staff_required
def student_registration_fee_entry():
    return render_template('student_registration_fee_entry.html', user=current_user)

@app.route('/finance/register-student', methods=['POST'])
@finance_staff_required
def register_student_by_finance():
    full_name = request.form.get('full_name', '').strip()
    reg_number = request.form.get('reg_number', '').strip() or generate_registration_number()
    email = request.form.get('email', '').strip() or f"{reg_number.replace('/', '').lower()}@kiu.ac.ug"
    username = request.form.get('username', '').strip() or reg_number.replace('/', '').lower()

    if not full_name:
        flash('Student full name is required.', 'danger')
        return redirect(url_for('student_registration_fee_entry'))
    if User.query.filter((User.username == username) | (User.email == email)).first():
        flash('A student with that username or email already exists.', 'danger')
        return redirect(url_for('student_registration_fee_entry'))
    if StudentProfile.query.filter_by(reg_number=reg_number).first():
        flash('That registration number is already in use.', 'danger')
        return redirect(url_for('student_registration_fee_entry'))

    student = User(username=username, email=email, role='student', is_active=True)
    student.set_password('student123')
    db.session.add(student)
    db.session.flush()
    db.session.add(StudentProfile(
        user_id=student.id,
        reg_number=reg_number,
        full_name=full_name,
        course=request.form.get('course') or 'Bachelor of Information Technology',
        year_of_study=int(request.form.get('year_of_study') or 1)
    ))
    db.session.add(FeeLedger(user_id=student.id, total_billed=3200000.00, total_paid=0, outstanding=3200000.00))
    db.session.add(AuditLog(user_id=current_user.id, action='STUDENT_REGISTERED',
                            details=f'Registered {full_name} ({reg_number})', ip_address=request.remote_addr))
    db.session.commit()
    flash(f'Student {full_name} registered. Temporary password: student123', 'success')
    return redirect(url_for('student_registration_fee_entry'))

@app.route('/finance/payment-verification')
@finance_staff_required
def payment_verification():
    pending_transactions = Transaction.query.filter_by(status='Pending').order_by(Transaction.created_at.asc()).all()
    selected_transaction = pending_transactions[0] if pending_transactions else None
    return render_template('payment_verification.html', user=current_user,
                           pending_transactions=pending_transactions,
                           selected_transaction=selected_transaction)

@app.route('/finance/payment-verification/<int:transaction_id>/<action>', methods=['POST'])
@finance_staff_required
def verify_payment(transaction_id, action):
    transaction = Transaction.query.get_or_404(transaction_id)
    comment = request.form.get('comment', '').strip()
    if transaction.status != 'Pending':
        flash('That transaction has already been processed.', 'warning')
        return redirect(url_for('payment_verification'))

    if action == 'approve':
        transaction.status = 'Confirmed'
        transaction.confirmed_at = datetime.utcnow()
        if transaction.user.ledger:
            transaction.user.ledger.total_paid += transaction.amount
            transaction.user.ledger.update_balance()
            transaction.user.ledger.last_updated = datetime.utcnow()
        audit_action = 'PAYMENT_APPROVED'
        flash('Payment approved and posted to the student ledger.', 'success')
    elif action == 'reject':
        transaction.status = 'Rejected'
        audit_action = 'PAYMENT_REJECTED'
        flash('Payment rejected and flagged for review.', 'warning')
    else:
        flash('Unknown verification action.', 'danger')
        return redirect(url_for('payment_verification'))

    db.session.add(AuditLog(user_id=current_user.id, action=audit_action,
                            details=f'{transaction.transaction_id}: {comment}', ip_address=request.remote_addr))
    db.session.commit()
    return redirect(url_for('payment_verification'))

@app.route('/finance/manual-record-entry', methods=['GET', 'POST'])
@finance_staff_required
def manual_record_entry():
    if request.method == 'POST':
        identifier = request.form.get('student_identifier') or request.form.get('reg_number') or request.form.get('student_name')
        student = find_student(identifier)
        if not student:
            flash('Student not found. Use username, email, registration number, or full name.', 'danger')
            return redirect(url_for('manual_record_entry'))

        try:
            amount = float(request.form.get('amount') or 0)
        except ValueError:
            amount = 0
        if amount <= 0:
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('manual_record_entry'))

        category = request.form.get('category') or 'Manual Payment'
        reference = request.form.get('reference') or generate_transaction_id()
        transaction = post_payment(student, amount, 'Manual Entry', f'{category} recorded by finance office',
                                   status='Confirmed', reference=reference, detail='Finance desk')
        db.session.add(AuditLog(user_id=current_user.id, action='MANUAL_PAYMENT_POSTED',
                                details=f'{transaction.transaction_id} for {student.username}', ip_address=request.remote_addr))
        db.session.commit()
        flash(f'Manual payment of UGX {amount:,.2f} posted for {student.username}.', 'success')
        return redirect(url_for('manual_record_entry'))

    recent_manual_entries = Transaction.query.filter_by(method='Manual Entry').order_by(Transaction.created_at.desc()).limit(10).all()
    return render_template('manual_record_entry.html', user=current_user, recent_manual_entries=recent_manual_entries)

@app.route('/finance/student-assistant')
@finance_staff_required
def student_assistant():
    return render_template('student_assistent.html', user=current_user)

@app.route('/finance/transaction-management')
@finance_staff_required
def transaction_management():
    page = request.args.get('page', 1, type=int)
    per_page = 10
    pagination = Transaction.query.order_by(Transaction.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('transaction_management.html',
                           user=current_user,
                           transactions=pagination.items,
                           pagination=pagination)

@app.route('/finance/daily-report')
@finance_staff_required
def daily_report():
    today = datetime.now().date()
    start_of_day = datetime(today.year, today.month, today.day)
    end_of_day = start_of_day + timedelta(days=1)
    today_transactions = Transaction.query.filter(
        Transaction.created_at >= start_of_day,
        Transaction.created_at < end_of_day,
        Transaction.status == 'Confirmed'
    ).all()
    today_total = sum(t.amount for t in today_transactions)
    today_count = len(today_transactions)
    
    return render_template('daily_report.html',
                          user=current_user,
                          today_total=today_total,
                          today_count=today_count)

@app.route('/finance/collection-report')
@finance_staff_required
def finance_collection_report():
    transactions = Transaction.query.filter_by(status='Confirmed').order_by(Transaction.created_at.desc()).all()
    total_amount = sum(t.amount for t in transactions)
    
    return render_template('finance_collection_report.html',
                          user=current_user,
                          transactions=transactions,
                          total_amount=total_amount)

@app.route('/finance/outstanding-report')
@finance_staff_required
def finance_outstanding_report():
    students_with_debt = db.session.query(User, FeeLedger).join(FeeLedger, User.id == FeeLedger.user_id).filter(FeeLedger.outstanding > 0).all()
    total_outstanding = sum(ledger.outstanding for user, ledger in students_with_debt)
    
    return render_template('finance_outstanding_report.html',
                          user=current_user,
                          students_with_debt=students_with_debt,
                          total_outstanding=total_outstanding)

@app.route('/finance/trends-report')
@finance_staff_required
def finance_trends_report():
    monthly_trends = db.session.query(
        db.func.strftime('%Y-%m', Transaction.created_at).label('month'),
        db.func.count(Transaction.id).label('count'),
        db.func.sum(Transaction.amount).label('total')
    ).filter(Transaction.status == 'Confirmed').group_by('month').order_by('month').limit(6).all()
    
    return render_template('finance_trends_report.html',
                          user=current_user,
                          monthly_trends=monthly_trends)

# ------------------------------
# STUDENT MODULE ROUTES
# ------------------------------
@app.route('/make-payment', methods=['GET', 'POST'])
@role_required('student')
def make_payment():
    if request.method == 'POST':
        try:
            amount = normalize_payment_amount(current_user, float(request.form.get('amount', 0)))
        except (TypeError, ValueError) as exc:
            flash(str(exc) if str(exc) else 'Invalid amount', 'danger')
            return redirect(url_for('make_payment'))


        payment_method = request.form.get('payment_method', 'mtn')
        back_url = request.form.get('back_url') or url_for('dashboard')
        session['payment_back_url'] = back_url

        try:
            if payment_method in ('mtn', 'airtel'):
                phone_number = request.form.get('phone_number')
                transaction = simulate_mobile_money_payment(current_user, amount, payment_method, phone_number)
                audit_action = 'DEMO_MOBILE_MONEY_PAYMENT'
                flash_message = f'Payment of UGX {amount:,.2f} successful!'
            elif payment_method == 'card':
                card_number = ''.join(ch for ch in (request.form.get('card_number') or '') if ch.isdigit())
                if len(card_number) < 12:
                    flash('Please enter a valid card number.', 'warning')
                    return redirect(url_for('make_payment'))
                card_last4 = card_number[-4:]
                transaction = post_payment(
                    current_user,
                    amount,
                    'Card',
                    f'Demo card payment of UGX {amount:,.2f}',
                    status='Confirmed',
                    reference=f"SIM-CARD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(100, 999)}",
                    detail=f'Card ending {card_last4}'
                )
                
                audit_action = 'DEMO_CARD_PAYMENT'
                flash_message = f'Card payment of UGX {amount:,.2f} successful!'
            elif payment_method == 'bank':
                bank_name = request.form.get('bank_name') or 'Direct Bank Pay'
                bank_reference = request.form.get('bank_reference') or generate_transaction_id()
                transaction = post_payment(
                    current_user,
                    amount,
                    'Bank Transfer',
                    f'Demo {bank_name} transfer of UGX {amount:,.2f}',
                    status='Confirmed',
                    reference=f"BANK-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(100, 999)}",
                    detail=bank_reference
                )
                audit_action = 'DEMO_BANK_PAYMENT'
                flash_message = f'{bank_name} payment of UGX {amount:,.2f} successful!'
            elif payment_method == 'proof':
                proof_filename = save_payment_proof(request.files.get('proof_file'))
                if not proof_filename:
                    flash('Please upload a valid proof file.', 'warning')
                    return redirect(url_for('make_payment'))
                proof_reference = request.form.get('proof_reference') or generate_transaction_id()
                transaction = post_payment(
                    current_user,
                    amount,
                    'Proof Upload',
                    f'Payment proof uploaded: {proof_filename}',
                    status='Pending',
                    reference=f"PROOF-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(100, 999)}",
                    detail=proof_reference[:50]
                )
                audit_action = 'PAYMENT_PROOF_UPLOADED'
                flash_message = 'Payment proof uploaded for finance verification.'
            else:
                flash('Unsupported payment method.', 'danger')
                return redirect(url_for('make_payment'))
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('make_payment'))

        db.session.add(AuditLog(user_id=current_user.id, action=audit_action,
                                details=f'{transaction.method}: {transaction.transaction_id}', ip_address=request.remote_addr))
        db.session.commit()

        flash(flash_message, 'success')
        return redirect(url_for('payment_confirmation', tx_id=transaction.transaction_id))

    previous_page = request.args.get('next') or request.referrer or url_for('dashboard')
    if previous_page.endswith(url_for('make_payment')):
        previous_page = url_for('dashboard')

    return render_template('make_payment_dashboard.html', user=current_user, ledger=current_user.ledger,
                           has_mtn_creds=True, credentials=None,
                           fee_breakdown=STUDENT_FEE_BREAKDOWN, total_dues=STUDENT_TOTAL_DUES,
                           previous_page=previous_page)

@app.route('/payment-confirmation/<tx_id>')
@role_required('student')
def payment_confirmation(tx_id):
    transaction = Transaction.query.filter_by(transaction_id=tx_id, user_id=current_user.id).first_or_404()
    back_url = request.args.get('next') or session.get('payment_back_url') or url_for('dashboard')
    return render_template('payment_confirmation.html', transaction=transaction, ledger=current_user.ledger, back_url=back_url)

@app.route('/transaction-history')
@role_required('student')
def transaction_history():
    page = request.args.get('page', 1, type=int)
    per_page = 10
    pagination = current_user.transactions.order_by(Transaction.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return render_template('transaction_history.html', transactions=pagination.items, pagination=pagination, user=current_user)

@app.route('/transaction-history/export')
@role_required('student')
def export_student_transactions():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Transaction ID', 'Date', 'Method', 'Description', 'Amount', 'Status'])
    for transaction in current_user.transactions.order_by(Transaction.created_at.desc()).all():
        writer.writerow([
            transaction.transaction_id,
            transaction.created_at.strftime('%Y-%m-%d %H:%M') if transaction.created_at else '',
            transaction.method,
            transaction.description,
            transaction.amount,
            transaction.status
        ])
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=my_transactions.csv'})

@app.route('/exam-card')
@role_required('student')
def exam_card():
    threshold = get_fee_threshold_status(current_user)
    if not threshold['exam_eligible']:
        return render_template('exam_card.html',
                               user=current_user,
                               ledger=current_user.ledger,
                               exam_eligible=False,
                               percent_paid=threshold['percent_paid'],
                               total_dues=STUDENT_TOTAL_DUES)
    
    card = ExamCard.query.filter_by(user_id=current_user.id, semester='Semester II').first()
    if not card:
        cn       = f"KIU-EC-{uuid.uuid4().hex[:8].upper()}"
        reg_num  = current_user.profile.reg_number if current_user.profile else str(current_user.id)
        qr_data  = f"{APP_BASE_URL}/verify-card/{cn}|{reg_num}"
        qr_path  = generate_qr_code(qr_data, f"{cn}.png")
        card = ExamCard(
            user_id=current_user.id,
            semester='Semester II',
            academic_year='2025/2026',
            card_number=cn,
            qr_code_data=qr_data,
            qr_code_path=qr_path,
            status='generated'
        )
        db.session.add(card)
        notify(current_user.id, f'Examination card {cn} generated for Semester II 2025/2026.')
        db.session.add(AuditLog(
            user_id=current_user.id, action='EXAM_CARD_GENERATED',
            details=f'Card {cn}', ip_address=request.remote_addr
        ))
        db.session.commit()
    return render_template('examination_card_clearance.html',
                           user=current_user, profile=current_user.profile, card=card)

@app.route('/exam-card/download')
@role_required('student')
def download_exam_card():
    threshold = get_fee_threshold_status(current_user)
    if not threshold['exam_eligible']:
        flash('Exam card download unlocks after 100% fee clearance.', 'warning')
        return redirect(url_for('exam_card'))

    card = ExamCard.query.filter_by(user_id=current_user.id, semester='Semester II').first()
    if not card:
        card = ExamCard(
            user_id=current_user.id,
            semester='Semester II',
            academic_year='2025/2026',
            qr_code_data=f'KIU-{current_user.id}-{datetime.now().strftime("%Y%m")}'
        )
        db.session.add(card)
        db.session.commit()

    html = render_template('examination_card_clearance.html', user=current_user, profile=current_user.profile, card=card)
    return Response(html, mimetype='text/html',
                    headers={'Content-Disposition': 'attachment; filename=kiu_exam_card.html'})

@app.route('/semester-registration', methods=['GET', 'POST'])
@role_required('student')
def semester_registration():
    threshold = get_fee_threshold_status(current_user)
    if request.method == 'POST':
        if not threshold['reg_eligible']:
            flash(f'Semester registration unlocks after paying at least {SEMESTER_REGISTRATION_THRESHOLD}% of fees.', 'warning')
            return redirect(url_for('semester_registration'))
        selected_courses = request.form.getlist('courses')
        if not selected_courses:
            flash('Select at least one course to register.', 'warning')
            return redirect(url_for('semester_registration'))

        existing = Registration.query.filter_by(
            user_id=current_user.id,
            semester='Semester II',
            academic_year='2025/2026'
        ).first()
        if not existing:
            registration = Registration(
                user_id=current_user.id,
                semester='Semester II',
                academic_year='2025/2026',
                courses_registered=', '.join(selected_courses),
                is_confirmed=True
            )
            db.session.add(registration)
        else:
            existing.courses_registered = ', '.join(selected_courses)
        db.session.commit()
        flash('Semester registration form generated successfully.', 'success')
        return redirect(url_for('semester_registration'))

    registration = Registration.query.filter_by(
        user_id=current_user.id,
        semester='Semester II',
        academic_year='2025/2026'
    ).first()
    return render_template('semester_registration.html',
                           user=current_user,
                           ledger=current_user.ledger,
                           reg_eligible=threshold['reg_eligible'],
                           percent_paid=threshold['percent_paid'],
                           registration=registration,
                           total_dues=STUDENT_TOTAL_DUES,
                           registration_threshold=SEMESTER_REGISTRATION_THRESHOLD,
                           predefined_courses=PREDEFINED_COURSES)

@app.route('/fee-structure')
@role_required('student')
def fee_structure():
    fees = {
        'tuition': STUDENT_FEE_BREAKDOWN['tuition'],
        'functional_fees': STUDENT_FEE_BREAKDOWN['functional_fees'],
        'exam_fee': STUDENT_FEE_BREAKDOWN['exam_fee'],
        'total_dues': STUDENT_TOTAL_DUES,
    }
    return render_template('fee_structure_page.html', user=current_user, fees=fees)

@app.route('/financial-reports')
@role_required('student')
def financial_reports():
    transactions = current_user.transactions.order_by(Transaction.created_at.desc()).all()
    return render_template('financial_report.html', user=current_user, ledger=current_user.ledger,
                           transactions=transactions)

@app.route('/payment-deadlines')
@role_required('student')
def payment_deadlines():
    deadlines = [
        {'semester': 'Semester I', 'date': '30 Oct 2026', 'status': 'Open'},
        {'semester': 'Semester II', 'date': '30 Mar 2027', 'status': 'Pending'},
    ]
    return render_template('payment_deadlines.html', user=current_user, deadlines=deadlines)

@app.route('/mtn-setup', methods=['GET', 'POST'])
@role_required('student')
def mtn_setup():
    credentials = MTNCredential.query.filter_by(user_id=current_user.id).first()
    if request.method == 'POST':
        subscription_key = request.form.get('subscription_key')
        if not credentials:
            credentials = MTNCredential(user_id=current_user.id)
            db.session.add(credentials)
        credentials.subscription_key = subscription_key
        credentials.is_active = bool(subscription_key)
        db.session.commit()
        flash('MTN MoMo settings saved.', 'success')
        return redirect(url_for('mtn_setup'))
    return render_template('mtn_setup.html', user=current_user, credentials=credentials)

@app.route('/process-mtn-payment', methods=['POST'])
@role_required('student')
def process_mtn_payment():
    amount = float(request.form.get('amount', 0))
    phone_number = request.form.get('phone_number')
    provider = request.form.get('payment_method', 'mtn')
    session['payment_back_url'] = request.form.get('back_url') or url_for('dashboard')
    try:
        transaction = simulate_mobile_money_payment(current_user, amount, provider, phone_number)
    except ValueError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('make_payment'))
    db.session.add(transaction)
    db.session.add(AuditLog(user_id=current_user.id, action='DEMO_MOBILE_MONEY_PAYMENT',
                            details=f'{transaction.method}: {transaction.transaction_id}', ip_address=request.remote_addr))
    db.session.commit()
    flash(f'Demo {transaction.method} payment of UGX {transaction.amount:,.2f} successful.', 'success')
    return redirect(url_for('payment_confirmation', tx_id=transaction.transaction_id))

@app.route('/student-profile')
@role_required('student')
def student_profile():
    return render_template('student_profile.html', user=current_user, profile=current_user.profile)

@app.route('/help-center')
@login_required
def help_center():
    return render_template('student_help_center.html', user=current_user)

@app.route('/admin/financial-oversight')
@admin_required
def financial_oversight():
    return render_template('financial_oversight.html', user=current_user)

@app.route('/finance/export-report/<report_type>')
@finance_staff_required
def export_report(report_type):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Transaction ID', 'Student', 'Amount', 'Status', 'Date'])

    query = Transaction.query
    if report_type == 'collection':
        query = query.filter_by(status='Confirmed')
    elif report_type == 'outstanding':
        writer.writerow(['Outstanding report is available from the outstanding report page.'])
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=outstanding_report.csv'})

    for transaction in query.order_by(Transaction.created_at.desc()).all():
        writer.writerow([
            transaction.transaction_id,
            transaction.user.username if transaction.user else '',
            transaction.amount,
            transaction.status,
            transaction.created_at.strftime('%Y-%m-%d') if transaction.created_at else ''
        ])

    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={report_type}_report.csv'})

@app.route('/api/finance/summary')
@finance_staff_required
def api_finance_summary():
    summary = finance_summary()
    return jsonify({
        'total_students': summary['total_students'],
        'total_revenue': summary['total_revenue'],
        'total_collected': summary['total_collected'],
        'total_outstanding': summary['total_outstanding'],
        'pending_verifications': summary['pending_verifications'],
        'latest_transaction_id': summary['latest_transaction_id'],
        'latest_transaction_at': summary['latest_transaction_at'],
    })

@app.route('/api/finance/clearance/<identifier>')
@finance_staff_required
def api_verify_clearance(identifier):
    student = find_student(identifier)
    if not student:
        return jsonify({'error': 'Student not found'}), 404

    threshold = get_fee_threshold_status(student)
    ledger = student.ledger
    return jsonify({
        'student_id': student.id,
        'username': student.username,
        'full_name': student.profile.full_name if student.profile else student.username,
        'reg_number': student.profile.reg_number if student.profile else None,
        'total_billed': ledger.total_billed if ledger else 0,
        'total_paid': ledger.total_paid if ledger else 0,
        'outstanding': ledger.outstanding if ledger else 0,
        'percent_paid': threshold['percent_paid'],
        'semester_registration_allowed': threshold['reg_eligible'],
        'exam_card_allowed': threshold['exam_eligible'],
    })

@app.route('/admin/export/<data_type>')
@admin_required
def admin_raw_export(data_type):
    output = StringIO()
    writer = csv.writer(output)

    if data_type == 'users':
        writer.writerow(['ID', 'Username', 'Email', 'Role', 'Active', 'Created At'])
        for user in User.query.order_by(User.id).all():
            writer.writerow([user.id, user.username, user.email, user.role, user.is_active, user.created_at])
    elif data_type == 'transactions':
        writer.writerow(['ID', 'Transaction ID', 'Student', 'Method', 'Amount', 'Status', 'Created At'])
        for transaction in Transaction.query.order_by(Transaction.id).all():
            writer.writerow([
                transaction.id,
                transaction.transaction_id,
                transaction.user.username if transaction.user else '',
                transaction.method,
                transaction.amount,
                transaction.status,
                transaction.created_at
            ])
    elif data_type == 'ledgers':
        writer.writerow(['Student', 'Registration Number', 'Total Billed', 'Total Paid', 'Outstanding', 'Last Updated'])
        for user, ledger in db.session.query(User, FeeLedger).join(FeeLedger, User.id == FeeLedger.user_id).order_by(User.username).all():
            writer.writerow([
                user.username,
                user.profile.reg_number if user.profile else '',
                ledger.total_billed,
                ledger.total_paid,
                ledger.outstanding,
                ledger.last_updated
            ])
    elif data_type == 'audit-logs':
        writer.writerow(['ID', 'User', 'Action', 'Details', 'IP Address', 'Timestamp'])
        for log in AuditLog.query.order_by(AuditLog.timestamp.desc()).all():
            writer.writerow([log.id, log.user.username if log.user else '', log.action, log.details, log.ip_address, log.timestamp])
    else:
        flash('Unknown export type.', 'danger')
        return redirect(url_for('admin_dashboard'))

    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={data_type}.csv'})


def _safe_url_for(endpoint, **values):
    try:
        return url_for(endpoint, **values)
    except Exception:
        return None


@app.after_request
def activate_placeholder_buttons(response):
    if response.direct_passthrough:
        return response

    content_type = response.headers.get('Content-Type', '')
    if 'text/html' not in content_type.lower():
        return response

    html = response.get_data(as_text=True)
    if '</body>' not in html or 'data-kiu-button-activator' in html:
        return response

    routes = {
        name: url
        for name, url in {
            'dashboard': _safe_url_for('dashboard'),
            'adminDashboard': _safe_url_for('admin_dashboard'),
            'financeDashboard': _safe_url_for('finance_dashboard'),
            'makePayment': _safe_url_for('make_payment'),
            'transactionHistory': _safe_url_for('transaction_history'),
            'transactionExport': _safe_url_for('export_student_transactions'),
            'transactionManagement': _safe_url_for('transaction_management'),
            'examCard': _safe_url_for('exam_card'),
            'examCardDownload': _safe_url_for('download_exam_card'),
            'semesterRegistration': _safe_url_for('semester_registration'),
            'feeStructure': _safe_url_for('fee_structure'),
            'manageFeeStructure': _safe_url_for('manage_fee_structure'),
            'financialReports': _safe_url_for('financial_reports'),
            'financeReports': _safe_url_for('finance_admin_reports'),
            'collectionReport': _safe_url_for('finance_collection_report'),
            'outstandingReport': _safe_url_for('finance_outstanding_report'),
            'trendsReport': _safe_url_for('finance_trends_report'),
            'dailyReport': _safe_url_for('daily_report'),
            'paymentDeadlines': _safe_url_for('payment_deadlines'),
            'paymentVerification': _safe_url_for('payment_verification'),
            'manualRecordEntry': _safe_url_for('manual_record_entry'),
            'studentRegistration': _safe_url_for('student_registration_fee_entry'),
            'studentAssistant': _safe_url_for('student_assistant'),
            'studentProfile': _safe_url_for('student_profile'),
            'helpCenter': _safe_url_for('help_center'),
            'userManagement': _safe_url_for('user_management'),
            'roleManagement': _safe_url_for('role_management'),
            'systemConfiguration': _safe_url_for('system_configuration'),
            'auditLogs': _safe_url_for('audit_logs'),
            'financialOversight': _safe_url_for('financial_oversight'),
            'mtnSetup': _safe_url_for('mtn_setup'),
            'logout': _safe_url_for('logout'),
            'collectionExport': _safe_url_for('export_report', report_type='collection'),
            'outstandingExport': _safe_url_for('export_report', report_type='outstanding'),
            'reportSummary': _safe_url_for('report_summary'),
            'reportOutstanding': _safe_url_for('report_outstanding'),
            'reportCleared': _safe_url_for('report_cleared'),
            'reportPaymentHistory': _safe_url_for('report_payment_history'),
            'reportFailed': _safe_url_for('report_failed'),
            'notifications': _safe_url_for('notifications'),
            'verifyCard': _safe_url_for('verify_card', card_number='DEMO'),
        }.items()
        if url
    }

    route_script = f"""
<script data-kiu-button-activator>
(function () {{
  const routes = {json.dumps(routes)};
  const rules = [
    [/logout|sign out/i, routes.logout],
    [/setting|configuration|setup/i, routes.systemConfiguration || routes.mtnSetup],
    [/notification|bell|audit|log|history icon/i, routes.auditLogs || routes.transactionHistory],
    [/support|help|chat|conversation|email finance|contact|troubleshoot|privacy|terms|handbook|policy/i, routes.helpCenter],
    [/admin/i, routes.adminDashboard],
    [/dashboard|overview/i, routes.financeDashboard || routes.dashboard],
    [/student registration|register student/i, routes.studentRegistration],
    [/registration form|semester|university registration/i, routes.semesterRegistration],
    [/make payment|payment|pay|mtn|airtel|card|prn/i, routes.makePayment],
    [/verify|verification|queue|pending|approved|flagged|reject/i, routes.paymentVerification],
    [/manual|record entry|cash/i, routes.manualRecordEntry],
    [/transaction management/i, routes.transactionManagement],
    [/transaction|statement|receipt|history/i, routes.transactionHistory],
    [/exam|clearance|permit|provisional card/i, routes.examCard],
    [/download.*card|exam.*download/i, routes.examCardDownload || routes.examCard],
    [/collection/i, routes.collectionReport || routes.financeReports],
    [/outstanding|overdue|balance/i, routes.outstandingReport || routes.financeReports],
    [/trend|forecast/i, routes.trendsReport || routes.financeReports],
    [/daily|archive/i, routes.dailyReport],
    [/report|pdf|print|export/i, routes.financeReports || routes.financialReports],
    [/fee structure|tuition schedule|program|global update|fee/i, routes.manageFeeStructure || routes.feeStructure],
    [/deadline/i, routes.paymentDeadlines],
    [/profile|account/i, routes.studentProfile],
    [/user|create/i, routes.userManagement],
    [/role|permission|apply update|discard/i, routes.roleManagement],
    [/oversight|finalize|period|review all|details|open_in_new/i, routes.financialOversight],
    [/assistant/i, routes.studentAssistant]
  ];

  function textFor(el) {{
    const iconText = Array.from(el.querySelectorAll('.material-symbols-outlined, [data-icon]'))
      .map((node) => node.getAttribute('data-icon') || node.textContent || '')
      .join(' ');
    return [
      el.getAttribute('aria-label'),
      el.getAttribute('title'),
      el.dataset.action,
      el.dataset.route,
      iconText,
      el.textContent
    ].filter(Boolean).join(' ').replace(/_/g, ' ').trim();
  }}

  function routeFor(el) {{
    const text = textFor(el);
    for (const [pattern, url] of rules) {{
      if (url && pattern.test(text)) return url;
    }}
    return null;
  }}

  function wireLink(link) {{
    const href = (link.getAttribute('href') || '').trim();
    if (href && href !== '#') return;
    const url = routeFor(link);
    if (url) link.setAttribute('href', url);
  }}

  function wireButton(button) {{
    if (button.disabled || button.dataset.kiuActivated || button.closest('form')) return;
    const type = (button.getAttribute('type') || 'button').toLowerCase();
    if (type === 'submit' || type === 'reset' || button.getAttribute('onclick')) return;

    const text = textFor(button);
    if (/print/i.test(text)) {{
      button.addEventListener('click', () => window.print());
      button.dataset.kiuActivated = 'print';
      return;
    }}

    if (/download|export/i.test(text)) {{
      const exportUrl =
        /outstanding/i.test(text) ? routes.outstandingExport :
        /card|exam/i.test(text) ? routes.examCardDownload :
        /transaction|history|statement/i.test(text) ? routes.transactionExport :
        routes.collectionExport || routes.transactionExport || routes.financeReports;
      if (exportUrl) {{
        button.addEventListener('click', () => window.location.assign(exportUrl));
        button.dataset.kiuActivated = 'download';
        return;
      }}
    }}

    if (/filter|search/i.test(text)) {{
      button.addEventListener('click', () => {{
        const form = button.closest('form') || document.querySelector('form');
        if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
        else document.querySelector('input[type="search"], input[placeholder*="Search" i]')?.focus();
      }});
      button.dataset.kiuActivated = 'filter';
      return;
    }}

    const url = routeFor(button);
    if (url) {{
      button.addEventListener('click', () => window.location.assign(url));
      button.dataset.kiuActivated = 'route';
    }}
  }}

  document.addEventListener('DOMContentLoaded', () => {{
    document.querySelectorAll('a[href="#"]').forEach(wireLink);
    document.querySelectorAll('button').forEach(wireButton);
  }});
}})();
</script>
"""

    response.set_data(html.replace('</body>', route_script + '\n</body>', 1))
    response.headers['Content-Length'] = str(len(response.get_data()))
    return response

# -------------------------------------------------------
# PUBLIC: QR Card Verification
# -------------------------------------------------------
@app.route('/verify-card/<card_number>')
def verify_card(card_number):
    card = ExamCard.query.filter_by(card_number=card_number).first()
    if not card:
        return render_template('verify_card.html', valid=False,
                               reason='Card not found in the system.', card_number=card_number)
    if card.status == 'revoked':
        return render_template('verify_card.html', valid=False,
                               reason='This card has been revoked by the Finance Office.', card=card)
    student = User.query.get(card.user_id)
    return render_template('verify_card.html', valid=True,
                           card=card, student=student,
                           profile=student.profile if student else None)


# -------------------------------------------------------
# Notifications
# -------------------------------------------------------
@app.route('/notifications')
@login_required
def notifications():
    notes = Notification.query.filter_by(user_id=current_user.id).order_by(
        Notification.created_at.desc()).limit(50).all()
    # Mark all as read
    Notification.query.filter_by(user_id=current_user.id, status='unread').update({'status': 'read'})
    db.session.commit()
    return render_template('notifications.html', user=current_user, notifications=notes)


@app.route('/api/notifications/count')
@login_required
def notification_count():
    count = Notification.query.filter_by(user_id=current_user.id, status='unread').count()
    return jsonify({'unread': count})


# -------------------------------------------------------
# Simulation Callback (Finance Admin triggers this)
# -------------------------------------------------------
@app.route('/finance/simulate-callback/<int:transaction_id>/<action>', methods=['POST'])
@finance_staff_required
@csrf.exempt
def simulate_payment_callback(transaction_id, action):
    """SIMULATION MODE: Finance admin marks pending mobile money payment as completed/failed."""
    txn = Transaction.query.get_or_404(transaction_id)
    if txn.status != 'Pending':
        flash('Transaction already processed.', 'warning')
        return redirect(url_for('payment_verification'))
    if action == 'complete':
        txn.status       = 'Confirmed'
        txn.confirmed_at = datetime.utcnow()
        if txn.user and txn.user.ledger:
            txn.user.ledger.total_paid += txn.amount
            txn.user.ledger.update_balance()
            txn.user.ledger.last_updated = datetime.utcnow()
        notify(txn.user_id,
               f'[SIMULATION] Payment of UGX {txn.amount:,.0f} confirmed. Ref: {txn.transaction_id}')
        db.session.add(AuditLog(user_id=current_user.id, action='SIM_PAYMENT_COMPLETED',
                                details=txn.transaction_id, ip_address=request.remote_addr))
        flash(f'[SIMULATION] Payment {txn.transaction_id} marked as Confirmed.', 'success')
    else:
        txn.status = 'Failed'
        notify(txn.user_id, f'Payment {txn.transaction_id} failed. Please retry.')
        db.session.add(AuditLog(user_id=current_user.id, action='SIM_PAYMENT_FAILED',
                                details=txn.transaction_id, ip_address=request.remote_addr))
        flash(f'[SIMULATION] Payment {txn.transaction_id} marked as Failed.', 'warning')
    db.session.commit()
    return redirect(url_for('payment_verification'))


# -------------------------------------------------------
# Finance: Revoke / Reinstate Exam Card
# -------------------------------------------------------
@app.route('/finance/revoke-card/<int:card_id>', methods=['POST'])
@finance_staff_required
def revoke_exam_card(card_id):
    card = ExamCard.query.get_or_404(card_id)
    card.status = 'revoked' if card.status == 'generated' else 'generated'
    action = 'EXAM_CARD_REVOKED' if card.status == 'revoked' else 'EXAM_CARD_REINSTATED'
    db.session.add(AuditLog(user_id=current_user.id, action=action,
                            details=f'Card {card.card_number}', ip_address=request.remote_addr))
    notify(card.user_id, f'Your exam card {card.card_number} has been {card.status} by Finance Office.')
    db.session.commit()
    flash(f'Exam card {card.status}.', 'success')
    return redirect(url_for('payment_verification'))


# -------------------------------------------------------
# Reports Module
# -------------------------------------------------------
@app.route('/reports/summary')
@finance_staff_required
def report_summary():
    total_students = User.query.filter_by(role='student').count()
    cleared = FeeLedger.query.filter_by(status='cleared').count()
    partial = FeeLedger.query.filter_by(status='partial').count()
    pending = FeeLedger.query.filter_by(status='pending').count()
    total_collected = db.session.query(db.func.sum(Transaction.amount)).filter_by(status='Confirmed').scalar() or 0
    total_outstanding = db.session.query(db.func.sum(FeeLedger.outstanding)).scalar() or 0
    return render_template('report_summary.html', user=current_user,
                           total_students=total_students, cleared=cleared,
                           partial=partial, pending=pending,
                           total_collected=total_collected,
                           total_outstanding=total_outstanding,
                           generated_at=datetime.now())


@app.route('/reports/outstanding')
@finance_staff_required
def report_outstanding():
    rows = db.session.query(User, FeeLedger, StudentProfile).join(
        FeeLedger, User.id == FeeLedger.user_id).outerjoin(
        StudentProfile, User.id == StudentProfile.user_id).filter(
        FeeLedger.outstanding > 0).order_by(FeeLedger.outstanding.desc()).all()
    total = sum(l.outstanding for _, l, _ in rows)
    return render_template('report_outstanding.html', user=current_user, rows=rows,
                           total=total, generated_at=datetime.now())


@app.route('/reports/cleared')
@finance_staff_required
def report_cleared():
    rows = db.session.query(User, FeeLedger, StudentProfile).join(
        FeeLedger, User.id == FeeLedger.user_id).outerjoin(
        StudentProfile, User.id == StudentProfile.user_id).filter(
        FeeLedger.status == 'cleared').all()
    return render_template('report_cleared.html', user=current_user, rows=rows,
                           generated_at=datetime.now())


@app.route('/reports/payment-history')
@finance_staff_required
def report_payment_history():
    page = request.args.get('page', 1, type=int)
    q    = Transaction.query.order_by(Transaction.created_at.desc())
    pagination = q.paginate(page=page, per_page=25, error_out=False)
    return render_template('report_payment_history.html', user=current_user,
                           transactions=pagination.items, pagination=pagination,
                           generated_at=datetime.now())


@app.route('/reports/failed')
@finance_staff_required
def report_failed():
    txns  = Transaction.query.filter(Transaction.status.in_(['Failed', 'Rejected'])).order_by(
        Transaction.created_at.desc()).all()
    total = sum(t.amount for t in txns)
    return render_template('report_failed.html', user=current_user, transactions=txns,
                           total=total, generated_at=datetime.now())


@app.route('/reports/export/<report_type>')
@finance_staff_required
def export_report_csv(report_type):
    output = StringIO()
    w = csv.writer(output)
    if report_type == 'summary':
        w.writerow(['Metric', 'Value'])
        summary = finance_summary()
        w.writerow(['Total Collected (UGX)', summary['total_collected']])
        w.writerow(['Total Outstanding (UGX)', summary['total_outstanding']])
        w.writerow(['Pending Verifications', summary['pending_verifications']])
    elif report_type == 'payment-history':
        w.writerow(['Ref', 'Student', 'Reg No.', 'Method', 'Amount', 'Status', 'Date'])
        for t in Transaction.query.order_by(Transaction.created_at.desc()).all():
            p = t.user.profile if t.user else None
            w.writerow([t.transaction_id, t.user.username if t.user else '',
                        p.reg_number if p else '', t.method, t.amount, t.status,
                        t.created_at.strftime('%Y-%m-%d %H:%M') if t.created_at else ''])
    elif report_type == 'outstanding':
        w.writerow(['Name', 'Reg No.', 'Total Fees', 'Paid', 'Outstanding', 'Status'])
        for u, l, p in db.session.query(User, FeeLedger, StudentProfile).join(
                FeeLedger, User.id == FeeLedger.user_id).outerjoin(
                StudentProfile, User.id == StudentProfile.user_id).filter(FeeLedger.outstanding > 0).all():
            w.writerow([p.full_name if p else u.username, p.reg_number if p else '',
                        l.total_billed, l.total_paid, l.outstanding, l.status])
    elif report_type == 'cleared':
        w.writerow(['Name', 'Reg No.', 'Total Fees', 'Paid'])
        for u, l, p in db.session.query(User, FeeLedger, StudentProfile).join(
                FeeLedger, User.id == FeeLedger.user_id).outerjoin(
                StudentProfile, User.id == StudentProfile.user_id).filter(
                FeeLedger.status == 'cleared').all():
            w.writerow([p.full_name if p else u.username, p.reg_number if p else '',
                        l.total_billed, l.total_paid])
    else:
        flash('Unknown report type.', 'danger')
        return redirect(url_for('finance_dashboard'))
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=kiu_{report_type}.csv'})


# -------------------------------------------------------
# Run
# -------------------------------------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        ensure_default_users()
        print('=' * 50)
        print('KIU Financial Management System')
        print('Admin: admin / admin123')
        print('Finance: finance / finance123')
        print('Student: student / student123')
        print('2FA OTPs print to console (simulation mode)')
        print('=' * 50)
    app.run(debug=True, host='0.0.0.0', port=5000)
