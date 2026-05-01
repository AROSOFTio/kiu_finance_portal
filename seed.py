"""
KIU Financial Management System — Seed Data
Run automatically by docker-entrypoint.sh on first start.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from app import app, db, bcrypt
from app import (User, StudentProfile, FeeLedger, Transaction,
                 FeeStructure, AuditLog, Notification,
                 generate_registration_number, generate_transaction_id, STUDENT_TOTAL_DUES)
from datetime import datetime, timedelta
import random

def seed():
    with app.app_context():
        db.create_all()

        # ── Helper ──────────────────────────────────────────────
        def make_user(username, email, password, role, is_admin=False, is_finance=False):
            u = User.query.filter_by(email=email).first()
            if not u:
                u = User(username=username, email=email, role=role,
                         is_admin=is_admin, is_finance_staff=is_finance, is_active=True)
                u.set_password(password)
                db.session.add(u)
                db.session.flush()
                print(f'  Created user: {username} ({role})')
            return u

        def make_profile(user, reg_num, full_name, course, year=2, semester='Semester II'):
            if not user.profile:
                db.session.add(StudentProfile(
                    user_id=user.id, reg_number=reg_num,
                    full_name=full_name, course=course,
                    year_of_study=year, semester=semester
                ))

        def make_ledger(user, total, paid):
            if not user.ledger:
                outstanding = max(0.0, total - paid)
                status = 'cleared' if outstanding <= 0 else ('partial' if paid > 0 else 'pending')
                db.session.add(FeeLedger(
                    user_id=user.id, total_billed=total,
                    total_paid=paid, outstanding=outstanding, status=status
                ))

        def make_txn(user, amount, method, ref, status='Confirmed', days_ago=0):
            if not Transaction.query.filter_by(transaction_id=ref).first():
                db.session.add(Transaction(
                    user_id=user.id, transaction_id=ref,
                    amount=amount, method=method,
                    description=f'{method} payment of UGX {amount:,.0f}',
                    status=status,
                    created_at=datetime.utcnow() - timedelta(days=days_ago),
                    confirmed_at=datetime.utcnow() - timedelta(days=days_ago) if status == 'Confirmed' else None
                ))

        # ── 1. System Admin ──────────────────────────────────────
        admin = make_user('admin', 'admin@kiu.ac.ug', 'admin123',
                          'admin', is_admin=True)
        make_profile(admin, 'KIU/ADMIN/001', 'System Administrator', 'Administration')
        make_ledger(admin, 0, 0)

        # ── 2. Finance Admin ─────────────────────────────────────
        finance = make_user('finance', 'finance@kiu.ac.ug', 'finance123',
                            'finance_staff', is_finance=True)
        make_profile(finance, 'KIU/FIN/001', 'Finance Administrator', 'Finance Office')
        make_ledger(finance, 0, 0)

        db.session.flush()

        # ── 3. Students ──────────────────────────────────────────
        # Student 1: CLEARED (100% paid)
        s1 = make_user('alice.nakato', 'alice.nakato@student.kiu.ac.ug', 'student123', 'student')
        make_profile(s1, 'KIU/2025/10001', 'Alice Nakato', 'Bachelor of Science in Computer Science', 2)
        make_ledger(s1, 3200000, 3200000)

        # Student 2: PARTIAL (50% paid)
        s2 = make_user('bob.mwangi', 'bob.mwangi@student.kiu.ac.ug', 'student123', 'student')
        make_profile(s2, 'KIU/2025/10002', 'Bob Mwangi', 'Bachelor of Information Technology', 1)
        make_ledger(s2, 3200000, 1600000)

        # Student 3: PENDING (no payment)
        s3 = make_user('carol.auma', 'carol.auma@student.kiu.ac.ug', 'student123', 'student')
        make_profile(s3, 'KIU/2025/10003', 'Carol Auma', 'Bachelor of Business Administration', 3)
        make_ledger(s3, 3200000, 0)

        db.session.flush()

        # ── 4. Fee Structures ────────────────────────────────────
        fee_entries = [
            ('2025/2026', 'Semester I', 'Bachelor of Science in Computer Science', 3200000,
             datetime(2025, 9, 30), 50000),
            ('2025/2026', 'Semester I', 'Bachelor of Information Technology', 3200000,
             datetime(2025, 9, 30), 50000),
            ('2025/2026', 'Semester I', 'Bachelor of Business Administration', 2800000,
             datetime(2025, 9, 30), 40000),
            ('2025/2026', 'Semester II', 'Bachelor of Science in Computer Science', 3200000,
             datetime(2026, 3, 31), 50000),
        ]
        for ay, sem, prog, amt, due, pen in fee_entries:
            if not FeeStructure.query.filter_by(academic_year=ay, semester=sem, programme=prog).first():
                db.session.add(FeeStructure(
                    academic_year=ay, semester=sem, programme=prog,
                    amount=amt, due_date=due, late_penalty=pen
                ))

        # ── 5. Transactions ──────────────────────────────────────
        # Alice (cleared) - 3 payments totalling 3,200,000
        make_txn(s1, 1500000, 'MTN MoMo',   'SIM-MTN-20250910-001', 'Confirmed', days_ago=120)
        make_txn(s1, 1200000, 'Airtel Money','SIM-AIR-20251015-002', 'Confirmed', days_ago=90)
        make_txn(s1,  500000, 'Bank Transfer','BANK-20251120-003',    'Confirmed', days_ago=60)

        # Bob (partial) - 1 payment of 1,600,000
        make_txn(s2, 1600000, 'MTN MoMo',   'SIM-MTN-20250915-004', 'Confirmed', days_ago=105)
        # Bob pending payment
        make_txn(s2,  400000, 'MTN MoMo',   'SIM-MTN-20260101-005', 'Pending',   days_ago=5)

        # Failed transaction example
        make_txn(s3,  800000, 'Airtel Money','SIM-AIR-20251201-006', 'Failed',    days_ago=45)

        # ── 6. Audit Logs ────────────────────────────────────────
        for u, action, detail in [
            (admin,   'LOGIN', 'System admin logged in (seed)'),
            (finance, 'LOGIN', 'Finance admin logged in (seed)'),
            (s1,      'LOGIN', 'Student Alice logged in (seed)'),
            (s1,      'PAYMENT', 'MTN payment of 1,500,000 confirmed'),
            (finance, 'PAYMENT_APPROVED', 'Approved bank payment for Alice'),
        ]:
            db.session.add(AuditLog(
                user_id=u.id, action=action,
                details=detail, ip_address='127.0.0.1',
                timestamp=datetime.utcnow() - timedelta(days=random.randint(1, 30))
            ))

        # ── 7. Notifications ─────────────────────────────────────
        if s1.id:
            db.session.add(Notification(
                user_id=s1.id,
                message='Your fees are fully paid. You are eligible to download your Examination Card.',
                type='system', status='unread'
            ))
        if s2.id:
            db.session.add(Notification(
                user_id=s2.id,
                message='You have an outstanding balance of UGX 1,600,000. Please clear fees before the exam period.',
                type='system', status='unread'
            ))

        db.session.commit()
        print('\n✅ Seed complete!')
        print('─' * 40)
        print('  admin@kiu.ac.ug       / admin123')
        print('  finance@kiu.ac.ug     / finance123')
        print('  alice.nakato@...      / student123  (CLEARED)')
        print('  bob.mwangi@...        / student123  (PARTIAL)')
        print('  carol.auma@...        / student123  (PENDING)')
        print('─' * 40)
        print('  2FA OTPs appear in Docker logs / console')
        print('─' * 40)

if __name__ == '__main__':
    seed()
