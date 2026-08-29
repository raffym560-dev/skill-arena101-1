from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
import sqlite3
import os
import secrets
import hashlib
import hmac
import json
import time
import base64
import urllib.parse


# ============================================================
# SKILL ARENA HUB
# COMPLETE SINGLE-FILE APPLICATION
# ============================================================

app = Flask(__name__)
CORS(app)

DB_NAME = os.environ.get("SKILL_ARENA_DB", "skill_arena.db")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "change-me-now"
)

APP_SECRET = os.environ.get(
    "SKILL_ARENA_SECRET",
    "CHANGE_THIS_SECRET_IN_PRODUCTION_9f82a1"
)

# ============================================================
# MANUAL PAYMENTS
# ============================================================
PAYMENT_PROVIDER = "manual"
PAYMENT_ACCOUNT_NUMBER = "03250150477"
PAYMENT_ACCOUNT_NAME = "Muhammad Raffy Umer"
PAYMENT_WALLET_NAME = "Nayapay"


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            identifier TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            fomo_coins INTEGER DEFAULT 20,
            referred_by TEXT,
            subscription_expires_at TEXT DEFAULT NULL,
            active_device_token TEXT DEFAULT NULL,
            instagram_followed INTEGER DEFAULT 0,
            is_demo INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            trx_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            amount INTEGER DEFAULT 0,
            hours INTEGER DEFAULT 1,
            reward INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Automatic gateway metadata. Safe to add to existing databases.
    existing_cols = {r[1] for r in cur.execute("PRAGMA table_info(transactions)").fetchall()}
    for col, definition in {
        "provider": "TEXT DEFAULT 'manual'",
        "order_id": "TEXT",
        "gateway_tracker": "TEXT",
        "gateway_reference": "TEXT",
        "paid_amount": "INTEGER DEFAULT 0",
        "paid_at": "TEXT"
    }.items():
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE transactions ADD COLUMN {col} {definition}")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_order_id ON transactions(order_id) WHERE order_id IS NOT NULL")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_gateway_tracker ON transactions(gateway_tracker) WHERE gateway_tracker IS NOT NULL")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            method TEXT NOT NULL,
            account_title TEXT NOT NULL,
            account_number TEXT NOT NULL,
            fomo_spent INTEGER NOT NULL,
            amount_pkr REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT NOT NULL,
            reset_code TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS game_sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            game TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            completed INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS coin_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    # --------------------------------------------------------
    # Old database migration
    # --------------------------------------------------------

    cur.execute("PRAGMA table_info(users)")
    user_columns = [x["name"] for x in cur.fetchall()]

    if "subscription_expires_at" not in user_columns:
        cur.execute(
            "ALTER TABLE users ADD COLUMN subscription_expires_at TEXT"
        )

    if "active_device_token" not in user_columns:
        cur.execute(
            "ALTER TABLE users ADD COLUMN active_device_token TEXT"
        )

    if "instagram_followed" not in user_columns:
        cur.execute(
            "ALTER TABLE users ADD COLUMN instagram_followed INTEGER DEFAULT 0"
        )

    if "is_demo" not in user_columns:
        cur.execute(
            "ALTER TABLE users ADD COLUMN is_demo INTEGER DEFAULT 0"
        )

    if "created_at" not in user_columns:
        cur.execute(
            "ALTER TABLE users ADD COLUMN created_at TEXT"
        )

    # Old passwords may be plain text.
    # Convert them to secure hashes when possible.
    cur.execute("SELECT user_id, password FROM users")

    for row in cur.fetchall():

        password = row["password"]

        if password and not password.startswith(
            ("pbkdf2:", "scrypt:")
        ):
            cur.execute(
                "UPDATE users SET password=? WHERE user_id=?",
                (
                    generate_password_hash(password),
                    row["user_id"]
                )
            )

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def now_string():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def get_user(user_id):

    conn = db()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    conn.close()

    return row


def is_demo_user(user):
    return bool(user and int(user["is_demo"] or 0) == 1)


def add_coins(user_id, amount, reason):

    conn = db()

    conn.execute(
        """
        UPDATE users
        SET fomo_coins = MAX(0, fomo_coins + ?)
        WHERE user_id=?
        """,
        (amount, user_id)
    )

    conn.execute(
        """
        INSERT INTO coin_ledger
        (user_id, amount, reason)
        VALUES (?, ?, ?)
        """,
        (user_id, amount, reason)
    )

    conn.commit()
    conn.close()


def is_subscription_active(user_id):

    user = get_user(user_id)

    if not user:
        return False

    expiry = user["subscription_expires_at"]

    if not expiry:
        return False

    try:
        return datetime.now() < datetime.strptime(
            expiry,
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return False


def active_seconds(user_id):

    user = get_user(user_id)

    if not user or not user["subscription_expires_at"]:
        return 0

    try:
        expiry = datetime.strptime(
            user["subscription_expires_at"],
            "%Y-%m-%d %H:%M:%S"
        )

        remaining = int(
            (expiry - datetime.now()).total_seconds()
        )

        return max(0, remaining)

    except Exception:
        return 0


def audit(action, target="", details=""):

    conn = db()

    conn.execute(
        """
        INSERT INTO admin_audit
        (action,target,details)
        VALUES (?,?,?)
        """,
        (action, target, details)
    )

    conn.commit()
    conn.close()


# ============================================================
# ADMIN AUTH
# ============================================================

def admin_required(fn):

    @wraps(fn)
    def wrapped(*args, **kwargs):

        auth = request.authorization

        if (
            not auth
            or not hmac.compare_digest(
                auth.username,
                ADMIN_USER
            )
            or not hmac.compare_digest(
                auth.password,
                ADMIN_PASSWORD
            )
        ):

            return (
                "Admin authentication required",
                401,
                {
                    "WWW-Authenticate":
                    'Basic realm="Skill Arena Admin"'
                }
            )

        return fn(*args, **kwargs)

    return wrapped


# ============================================================
# SESSION VERIFICATION
# ============================================================

def valid_user_session(user_id, device_token):

    if not user_id or not device_token:
        return False

    user = get_user(user_id)

    if not user:
        return False

    return hmac.compare_digest(
        str(user["active_device_token"] or ""),
        str(device_token)
    )


def require_user(data):

    user_id = data.get("user_id")
    device_token = data.get("device_token")

    if not valid_user_session(
        user_id,
        device_token
    ):
        return None

    return get_user(user_id)


# ============================================================
# REGISTER
# ============================================================

@app.route("/api/register", methods=["POST"])
def register():

    data = request.get_json(silent=True) or {}

    identifier = str(
        data.get("identifier", "")
    ).strip().lower()

    password = str(
        data.get("password", "")
    )

    referrer = str(
        data.get("referrer_id", "")
    ).strip()

    if not identifier or not password:

        return jsonify({
            "success": False,
            "message":
            "❌ Email/Mobile aur Password lazmi hain."
        }), 400

    if len(password) < 6:

        return jsonify({
            "success": False,
            "message":
            "❌ Password kam az kam 6 characters ka hona chahiye."
        }), 400

    conn = db()

    exists = conn.execute(
        "SELECT user_id FROM users WHERE identifier=?",
        (identifier,)
    ).fetchone()

    if exists:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "⚠️ Account already registered. Login karein."
        }), 400

    user_id = (
        "USER_"
        + secrets.token_hex(4).upper()
    )

    device_token = (
        "DEV_"
        + secrets.token_hex(8).upper()
    )

    password_hash = generate_password_hash(
        password
    )

    # Validate referral
    if referrer:

        ref_exists = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (referrer,)
        ).fetchone()

        if not ref_exists:
            referrer = None

    conn.execute(
        """
        INSERT INTO users
        (
            user_id,
            identifier,
            password,
            fomo_coins,
            referred_by,
            active_device_token
        )
        VALUES (?,?,?,?,?,?)
        """,
        (
            user_id,
            identifier,
            password_hash,
            20,
            referrer,
            device_token
        )
    )

    conn.execute(
        """
        INSERT INTO coin_ledger
        (user_id,amount,reason)
        VALUES (?,?,?)
        """,
        (
            user_id,
            20,
            "Registration welcome bonus"
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message":
        "✅ Account created! 20 FOMO Coins welcome bonus.",
        "user_id": user_id,
        "device_token": device_token,
        "fomo_coins": 20
    })


# ============================================================
# DEMO LOGIN
# ============================================================

@app.route("/api/demo-login", methods=["POST"])
def demo_login():

    # Each visitor receives an isolated temporary demo user.
    demo_id = "DEMO_" + secrets.token_hex(12).upper()
    demo_identifier = "demo_" + secrets.token_hex(8).lower() + "@skillarena.local"
    demo_password = secrets.token_urlsafe(24)
    demo_hash = generate_password_hash(demo_password)
    new_device_token = "DEMO_DEV_" + secrets.token_hex(16).upper()
    demo_expiry = None

    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO users
            (user_id, identifier, password, fomo_coins,
             subscription_expires_at, active_device_token, is_demo)
            VALUES (?, ?, ?, 0, ?, ?, 1)
            """,
            (demo_id, demo_identifier, demo_hash, demo_expiry, new_device_token)
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        "success": True,
        "message": "🎮 Demo Mode started. FOMO rewards are disabled.",
        "user_id": demo_id,
        "device_token": new_device_token,
        "fomo_coins": 0,
        "demo_mode": True
    })


# ============================================================
# LOGIN
# ============================================================

@app.route("/api/login", methods=["POST"])
def login():

    data = request.get_json(silent=True) or {}

    identifier = str(
        data.get("identifier", "")
    ).strip().lower()

    password = str(
        data.get("password", "")
    )

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE identifier=?
        """,
        (identifier,)
    ).fetchone()

    if not row:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "❌ Galat Email/Mobile ya Password."
        }), 400

    stored = row["password"]

    password_ok = False

    try:

        password_ok = check_password_hash(
            stored,
            password
        )

    except Exception:
        password_ok = False

    # Old plain-text database compatibility
    if not password_ok and hmac.compare_digest(
        str(stored),
        password
    ):

        password_ok = True

        new_hash = generate_password_hash(
            password
        )

        conn.execute(
            """
            UPDATE users
            SET password=?
            WHERE user_id=?
            """,
            (
                new_hash,
                row["user_id"]
            )
        )

    if not password_ok:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "❌ Galat Email/Mobile ya Password."
        }), 400

    new_device_token = (
        "DEV_"
        + secrets.token_hex(8).upper()
    )

    conn.execute(
        """
        UPDATE users
        SET active_device_token=?
        WHERE user_id=?
        """,
        (
            new_device_token,
            row["user_id"]
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message":
        "✅ Login successful. Other device session logout ho gayi.",
        "user_id": row["user_id"],
        "device_token": new_device_token,
        "fomo_coins": row["fomo_coins"]
    })


# ============================================================
# VERIFY SESSION
# ============================================================

@app.route("/api/verify-session", methods=["POST"])
def verify_session():

    data = request.get_json(silent=True) or {}

    if valid_user_session(
        data.get("user_id"),
        data.get("device_token")
    ):

        return jsonify({
            "valid": True
        })

    return jsonify({
        "valid": False,
        "message":
        "⚠️ Session expired ya doosri device par login hua hai."
    })


# ============================================================
# FORGOT PASSWORD
# ============================================================

@app.route(
    "/api/request-password-reset",
    methods=["POST"]
)
def request_password_reset():

    data = request.get_json(silent=True) or {}

    identifier = str(
        data.get("identifier", "")
    ).strip().lower()

    if not identifier:

        return jsonify({
            "success": False,
            "message":
            "❌ Email/Mobile enter karein."
        }), 400

    conn = db()

    user = conn.execute(
        """
        SELECT user_id
        FROM users
        WHERE identifier=?
        """,
        (identifier,)
    ).fetchone()

    # Do not reveal whether account exists
    if not user:

        conn.close()

        return jsonify({
            "success": True,
            "message":
            "Agar account exist karta hai to reset code generate ho gaya."
        })

    # Invalidate old codes
    conn.execute(
        """
        UPDATE password_resets
        SET used=1
        WHERE identifier=?
        AND used=0
        """,
        (identifier,)
    )

    code = str(
        secrets.randbelow(900000) + 100000
    )

    expires = (
        datetime.now()
        + timedelta(minutes=10)
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    conn.execute(
        """
        INSERT INTO password_resets
        (
            identifier,
            reset_code,
            expires_at
        )
        VALUES (?,?,?)
        """,
        (
            identifier,
            code,
            expires
        )
    )

    conn.commit()
    conn.close()

    # Local/testing mode.
    # Production mein email/SMS provider connect karna chahiye.
    return jsonify({
        "success": True,
        "message":
        "🔐 Reset code generate ho gaya. 10 minutes valid.",
        "reset_code": code
    })


@app.route(
    "/api/reset-password",
    methods=["POST"]
)
def reset_password():

    data = request.get_json(silent=True) or {}

    identifier = str(
        data.get("identifier", "")
    ).strip().lower()

    code = str(
        data.get("reset_code", "")
    ).strip()

    new_password = str(
        data.get("new_password", "")
    )

    if not identifier or not code or not new_password:

        return jsonify({
            "success": False,
            "message":
            "❌ Sab fields complete karein."
        }), 400

    if len(new_password) < 6:

        return jsonify({
            "success": False,
            "message":
            "❌ Password minimum 6 characters hona chahiye."
        }), 400

    conn = db()

    reset = conn.execute(
        """
        SELECT *
        FROM password_resets
        WHERE identifier=?
        AND reset_code=?
        AND used=0
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            identifier,
            code
        )
    ).fetchone()

    if not reset:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid ya already-used reset code."
        }), 400

    try:

        expiry = datetime.strptime(
            reset["expires_at"],
            "%Y-%m-%d %H:%M:%S"
        )

    except Exception:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid reset request."
        }), 400

    if datetime.now() > expiry:

        conn.execute(
            """
            UPDATE password_resets
            SET used=1
            WHERE id=?
            """,
            (reset["id"],)
        )

        conn.commit()
        conn.close()

        return jsonify({
            "success": False,
            "message":
            "⏰ Reset code expire ho gaya."
        }), 400

    new_hash = generate_password_hash(
        new_password
    )

    new_device_token = (
        "DEV_"
        + secrets.token_hex(8).upper()
    )

    conn.execute(
        """
        UPDATE users
        SET password=?,
            active_device_token=?
        WHERE identifier=?
        """,
        (
            new_hash,
            new_device_token,
            identifier
        )
    )

    conn.execute(
        """
        UPDATE password_resets
        SET used=1
        WHERE id=?
        """,
        (reset["id"],)
    )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message":
        "✅ Password successfully change ho gaya."
    })


# ============================================================
# MANUAL PAYMENT CREATION
# ============================================================

def _subscription_plan(price, hours):
    plans = {(50, 1), (100, 2), (180, 3)}
    return (price, hours) in plans


@app.route("/api/create-payment", methods=["POST"])
def create_payment():
    data = request.get_json(silent=True) or {}
    user = require_user(data)
    if not user:
        return jsonify({"success": False, "message": "❌ Invalid session."}), 401

    if is_demo_user(user):
        return jsonify({"success": False, "message": "🎮 Demo Mode mein real Pass purchase disabled hai."}), 403

    try:
        amount = int(data.get("price", 0))
        hours = int(data.get("hours", 0))
    except Exception:
        amount = hours = 0

    if not _subscription_plan(amount, hours):
        return jsonify({"success": False, "message": "❌ Invalid subscription plan."}), 400

    trx_id = str(data.get("trx_id", "")).strip().upper()
    if not trx_id:
        return jsonify({"success": False, "message": "❌ TRX ID enter karein."}), 400

    # NayaPay transaction IDs are manually checked by the admin.
    # The database primary key also prevents the same TRX ID from being
    # submitted repeatedly for another payment request.
    if len(trx_id) > 100:
        return jsonify({"success": False, "message": "❌ Invalid TRX ID."}), 400

    conn = db()
    try:
        existing = conn.execute(
            "SELECT status FROM transactions WHERE trx_id=?",
            (trx_id,)
        ).fetchone()

        if existing:
            if existing["status"] == "approved":
                msg = "❌ Ye TRX ID already approved hai."
            elif existing["status"] == "pending":
                msg = "⏳ Ye TRX ID already verification mein hai."
            else:
                msg = "❌ Ye TRX ID pehle submit ho chuki hai."
            return jsonify({"success": False, "message": msg}), 409

        conn.execute(
            """INSERT INTO transactions
               (trx_id,user_id,amount,hours,reward,status,provider)
               VALUES (?,?,?,?,?,'pending','manual')""",
            (trx_id, user["user_id"], amount, hours, 0)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({"success": False, "message": "❌ Ye TRX ID already submit ho chuki hai."}), 409
    finally:
        conn.close()

    audit(
        "PAYMENT_SUBMITTED",
        trx_id,
        f"user={user['user_id']},amount={amount},hours={hours}"
    )

    return jsonify({
        "success": True,
        "trx_id": trx_id,
        "message": "✅ Payment request submit ho gayi. Admin verification ke baad Pass activate hoga."
    })


# ============================================================
# PAYMENT STATUS
# ============================================================

@app.route(
    "/api/check-status/<trx_id>",
    methods=["GET"]
)
def check_status(trx_id):

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM transactions
        WHERE trx_id=?
        """,
        (trx_id.upper(),)
    ).fetchone()

    conn.close()

    if not row:

        return jsonify({
            "status": "not_found"
        })

    return jsonify({
        "status": row["status"],
        "hours": row["hours"],
        "reward": row["reward"],
        "user_id": row["user_id"]
    })


# ============================================================
# REDEEM 500 FOMO = 1 HOUR
# ============================================================

@app.route(
    "/api/redeem-pass",
    methods=["POST"]
)
def redeem_pass():

    data = request.get_json(silent=True) or {}

    user = require_user(data)

    if not user:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid session."
        }), 401

    if is_demo_user(user):
        return jsonify({"success": False, "message": "🎮 Demo Mode mein FOMO redemption disabled hai."}), 403

    try:
        coins = int(data.get("coins", 500))
    except (TypeError, ValueError):
        coins = 500

    redemption_hours = {
        500: 1,
        1000: 2,
        1500: 3,
        2000: 4
    }

    if coins not in redemption_hours:
        return jsonify({
            "success": False,
            "message":
            "❌ Invalid FOMO redemption option."
        })

    hours = redemption_hours[coins]

    if user["fomo_coins"] < coins:

        return jsonify({
            "success": False,
            "message":
            f"❌ {coins} FOMO Coins required."
        })

    now = datetime.now()

    expiry = user["subscription_expires_at"]

    if expiry:

        try:

            old_expiry = datetime.strptime(
                expiry,
                "%Y-%m-%d %H:%M:%S"
            )

            base = max(
                now,
                old_expiry
            )

        except Exception:

            base = now

    else:

        base = now

    new_expiry = (
        base + timedelta(hours=hours)
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    conn = db()

    conn.execute(
        """
        UPDATE users
        SET fomo_coins=fomo_coins-?,
            subscription_expires_at=?
        WHERE user_id=?
        AND fomo_coins>=?
        """,
        (
            coins,
            new_expiry,
            user["user_id"],
            coins
        )
    )

    conn.execute(
        """
        INSERT INTO coin_ledger
        (user_id,amount,reason)
        VALUES (?,?,?)
        """,
        (
            user["user_id"],
            -coins,
            f"{coins} FOMO redeemed for {hours}-hour pass"
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message":
        f"✅ {coins} FOMO redeemed for {hours}-hour pass.",
        "expires_at": new_expiry
    })


# ============================================================
# INSTAGRAM REWARD
# ============================================================

@app.route(
    "/api/claim-instagram-reward",
    methods=["POST"]
)
def instagram_reward():

    data = request.get_json(silent=True) or {}

    user = require_user(data)

    if not user:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid session."
        }), 401

    if is_demo_user(user):
        return jsonify({"success": False, "message": "🎮 Demo Mode mein FOMO rewards disabled hain."}), 403

    if user["instagram_followed"]:

        return jsonify({
            "success": False,
            "message":
            "⚠️ Instagram reward already claimed."
        })

    conn = db()

    conn.execute(
        """
        UPDATE users
        SET fomo_coins=fomo_coins+10,
            instagram_followed=1
        WHERE user_id=?
        """,
        (user["user_id"],)
    )

    conn.execute(
        """
        INSERT INTO coin_ledger
        (user_id,amount,reason)
        VALUES (?,?,?)
        """,
        (
            user["user_id"],
            10,
            "Instagram reward"
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message":
        "🎉 10 FOMO Coins added."
    })


# ============================================================
# WITHDRAWAL
# ============================================================

@app.route(
    "/api/submit-withdrawal",
    methods=["POST"]
)
def submit_withdrawal():

    data = request.get_json(silent=True) or {}

    user = require_user(data)

    if not user:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid session."
        }), 401

    if is_demo_user(user):
        return jsonify({"success": False, "message": "🎮 Demo Mode mein withdrawal disabled hai."}), 403

    method = str(
        data.get("method", "")
    ).strip()

    title = str(
        data.get("title", "")
    ).strip()

    account = str(
        data.get("acc", "")
    ).strip()

    try:
        coins = int(
            data.get("fomo_coins", 0)
        )
    except Exception:
        coins = 0

    if not method or not title or not account:

        return jsonify({
            "success": False,
            "message":
            "❌ Withdrawal details complete karein."
        }), 400

    if coins < 100:

        return jsonify({
            "success": False,
            "message":
            "❌ Minimum 100 FOMO Coins."
        }), 400

    if not is_subscription_active(
        user["user_id"]
    ):

        return jsonify({
            "success": False,
            "message":
            "❌ Active subscription ke andar hi withdrawal allowed hai."
        }), 400

    if user["fomo_coins"] < coins:

        return jsonify({
            "success": False,
            "message":
            "❌ Insufficient FOMO Coins."
        }), 400

    # Existing app's conversion
    amount_pkr = round(
        coins * 0.04,
        2
    )

    conn = db()

    # Atomic-ish balance protection
    cur = conn.execute(
        """
        UPDATE users
        SET fomo_coins=fomo_coins-?
        WHERE user_id=?
        AND fomo_coins>=?
        """,
        (
            coins,
            user["user_id"],
            coins
        )
    )

    if cur.rowcount != 1:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "❌ Coins update failed."
        }), 400

    conn.execute(
        """
        INSERT INTO withdrawals
        (
            user_id,
            method,
            account_title,
            account_number,
            fomo_spent,
            amount_pkr,
            status
        )
        VALUES (?,?,?,?,?,?, 'pending')
        """,
        (
            user["user_id"],
            method,
            title,
            account,
            coins,
            amount_pkr
        )
    )

    conn.execute(
        """
        INSERT INTO coin_ledger
        (user_id,amount,reason)
        VALUES (?,?,?)
        """,
        (
            user["user_id"],
            -coins,
            "Withdrawal request"
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message":
        "✅ Withdrawal request submitted."
    })


# ============================================================
# USER DATA
# ============================================================

@app.route(
    "/api/user-data/<user_id>",
    methods=["GET"]
)
def user_data(user_id):

    user = get_user(user_id)

    if not user:

        return jsonify({
            "success": False,
            "message":
            "User not found."
        }), 404

    seconds = active_seconds(
        user_id
    )

    return jsonify({
        "success": True,
        "user_id": user_id,
        "fomo_coins": user["fomo_coins"],
        "active_seconds": seconds,
        "instagram_followed":
            user["instagram_followed"],
        "demo_mode": is_demo_user(user)
    })


# ============================================================
# REFERRAL
# ============================================================

@app.route(
    "/api/apply-referral",
    methods=["POST"]
)
def apply_referral():

    data = request.get_json(silent=True) or {}

    user = require_user(data)

    if not user:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid session."
        }), 401

    if is_demo_user(user):
        return jsonify({"success": False, "message": "🎮 Demo Mode mein referral rewards disabled hain."}), 403

    referrer = str(
        data.get("referrer_id", "")
    ).strip()

    if not referrer or referrer == user["user_id"]:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid referral."
        }), 400

    if user["referred_by"]:

        return jsonify({
            "success": False,
            "message":
            "⚠️ Referral already applied."
        }), 400

    conn = db()

    ref = conn.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (referrer,)
    ).fetchone()

    if not ref:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid referral ID."
        }), 400

    conn.execute(
        """
        UPDATE users
        SET referred_by=?
        WHERE user_id=?
        """,
        (
            referrer,
            user["user_id"]
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message":
        "✅ Referral applied."
    })


# ============================================================
# GAME SECURITY
# ============================================================

GAME_REWARD = 15
GAME_LOSS = -5


def sign_token(raw):

    return hmac.new(
        APP_SECRET.encode(),
        raw.encode(),
        hashlib.sha256
    ).hexdigest()


def create_game_session(
    user_id,
    game,
    max_seconds=900
):

    raw = (
        user_id
        + "|"
        + game
        + "|"
        + secrets.token_hex(20)
    )

    token = (
        raw
        + "."
        + sign_token(raw)
    )

    now = int(time.time())

    conn = db()

    conn.execute(
        """
        INSERT INTO game_sessions
        (
            token,
            user_id,
            game,
            started_at,
            expires_at
        )
        VALUES (?,?,?,?,?)
        """,
        (
            token,
            user_id,
            game,
            now,
            now + max_seconds
        )
    )

    conn.commit()
    conn.close()

    return token


def verify_game_session(
    token,
    user_id,
    game
):

    if not token:
        return None

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM game_sessions
        WHERE token=?
        """,
        (token,)
    ).fetchone()

    conn.close()

    if not row:
        return None

    if row["completed"]:
        return None

    if row["user_id"] != user_id:
        return None

    if row["game"] != game:
        return None

    if int(time.time()) > row["expires_at"]:
        return None

    # Signature validation
    try:

        raw, signature = token.rsplit(
            ".",
            1
        )

        expected = sign_token(raw)

        if not hmac.compare_digest(
            signature,
            expected
        ):
            return None

    except Exception:
        return None

    return row


@app.route(
    "/api/game/start",
    methods=["POST"]
)
def game_start():

    data = request.get_json(silent=True) or {}

    user = require_user(data)

    if not user:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid session."
        }), 401

    # Demo Mode is free to play. Paid/registered users still need an active Pass.
    if not is_demo_user(user) and not is_subscription_active(
        user["user_id"]
    ):

        return jsonify({
            "success": False,
            "message":
            "⏰ Active Pass required."
        }), 400

    game = str(
        data.get("game", "")
    ).strip()

    allowed = {
        "slide",
        "memory",
        "word_escape",
        "word_search",
        "car_escape"
    }

    if game not in allowed:

        return jsonify({
            "success": False,
            "message":
            "Invalid game."
        }), 400

    token = create_game_session(
        user["user_id"],
        game
    )

    return jsonify({
        "success": True,
        "game_token": token
    })


@app.route(
    "/api/game/result",
    methods=["POST"]
)
def game_result():

    data = request.get_json(silent=True) or {}

    user = require_user(data)

    if not user:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid session."
        }), 401

    game = str(
        data.get("game", "")
    )

    token = data.get(
        "game_token"
    )

    result = str(
        data.get("result", "")
    ).lower()

    session = verify_game_session(
        token,
        user["user_id"],
        game
    )

    if not session:

        return jsonify({
            "success": False,
            "message":
            "❌ Invalid, expired or already-used game session."
        }), 400

    if result not in (
        "win",
        "lose"
    ):

        return jsonify({
            "success": False,
            "message":
            "Invalid result."
        }), 400

    # --------------------------------------------------------
    # Car Escape minimum time protection.
    # This does not make browser games impossible to cheat,
    # but prevents instant repeated reward calls.
    # --------------------------------------------------------

    elapsed = (
        int(time.time())
        - session["started_at"]
    )

    if game == "car_escape":

        if result == "win" and elapsed < 10:

            return jsonify({
                "success": False,
                "message":
                "❌ Car Escape result submitted too quickly."
            }), 400

    amount = (
        0
        if is_demo_user(user)
        else (GAME_REWARD if result == "win" else GAME_LOSS)
    )

    conn = db()

    # One-time completion
    cur = conn.execute(
        """
        UPDATE game_sessions
        SET completed=1
        WHERE token=?
        AND completed=0
        """,
        (token,)
    )

    if cur.rowcount != 1:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "❌ Game result already processed."
        }), 400

    conn.execute(
        """
        UPDATE users
        SET fomo_coins =
            MAX(0, fomo_coins + ?)
        WHERE user_id=?
        """,
        (
            amount,
            user["user_id"]
        )
    )

    reason = (
        f"{game} WIN"
        if result == "win"
        else f"{game} LOSE"
    )

    conn.execute(
        """
        INSERT INTO coin_ledger
        (user_id,amount,reason)
        VALUES (?,?,?)
        """,
        (
            user["user_id"],
            amount,
            reason
        )
    )

    conn.commit()

    new_user = conn.execute(
        """
        SELECT fomo_coins
        FROM users
        WHERE user_id=?
        """,
        (user["user_id"],)
    ).fetchone()

    conn.close()

    return jsonify({
        "success": True,
        "result": result,
        "points": amount,
        "fomo_coins": new_user["fomo_coins"]
    })


# ============================================================
# ADMIN: PAYMENT APPROVE
# ============================================================

@app.route(
    "/api/admin/approve",
    methods=["POST"]
)
@admin_required
def admin_approve():

    data = request.get_json(
        silent=True
    ) or {}

    trx_id = str(
        data.get("trx_id", "")
    ).upper()

    conn = db()

    trx = conn.execute(
        """
        SELECT *
        FROM transactions
        WHERE trx_id=?
        """,
        (trx_id,)
    ).fetchone()

    if not trx:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Transaction not found."
        }), 404

    if trx["status"] != "pending":

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Transaction already processed."
        }), 400

    user = conn.execute(
        """
        SELECT *
        FROM users
        WHERE user_id=?
        """,
        (trx["user_id"],)
    ).fetchone()

    if not user:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "User not found."
        }), 404

    now = datetime.now()

    if user["subscription_expires_at"]:

        try:

            current = datetime.strptime(
                user["subscription_expires_at"],
                "%Y-%m-%d %H:%M:%S"
            )

            base = max(
                now,
                current
            )

        except Exception:

            base = now

    else:

        base = now

    new_expiry = (
        base
        + timedelta(
            hours=trx["hours"]
        )
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # First approved purchase
    approved_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE user_id=?
        AND status='approved'
        """,
        (trx["user_id"],)
    ).fetchone()[0]

    conn.execute(
        """
        UPDATE transactions
        SET status='approved'
        WHERE trx_id=?
        """,
        (trx_id,)
    )

    conn.execute(
        """
        UPDATE users
        SET subscription_expires_at=?
        WHERE user_id=?
        """,
        (
            new_expiry,
            trx["user_id"]
        )
    )

    # Existing referral concept:
    # first subscription -> user +25,
    # referrer +25.
    if approved_count == 0:

        conn.execute(
            """
            UPDATE users
            SET fomo_coins=fomo_coins+25
            WHERE user_id=?
            """,
            (trx["user_id"],)
        )

        conn.execute(
            """
            INSERT INTO coin_ledger
            (user_id,amount,reason)
            VALUES (?,?,?)
            """,
            (
                trx["user_id"],
                25,
                "First subscription reward"
            )
        )

        ref = user["referred_by"]

        if ref:

            conn.execute(
                """
                UPDATE users
                SET fomo_coins=fomo_coins+25
                WHERE user_id=?
                """,
                (ref,)
            )

            conn.execute(
                """
                INSERT INTO coin_ledger
                (user_id,amount,reason)
                VALUES (?,?,?)
                """,
                (
                    ref,
                    25,
                    "Referral first-subscription reward"
                )
            )

    conn.commit()
    conn.close()

    audit(
        "PAYMENT_APPROVED",
        trx_id,
        f"user={trx['user_id']}"
    )

    return jsonify({
        "success": True,
        "message":
        "✅ Payment Approved + Pass Activated."
    })


# ============================================================
# ADMIN: PAYMENT REJECT
# ============================================================

@app.route(
    "/api/admin/reject",
    methods=["POST"]
)
@admin_required
def admin_reject():

    data = request.get_json(
        silent=True
    ) or {}

    trx_id = str(
        data.get("trx_id", "")
    ).upper()

    conn = db()

    row = conn.execute(
        """
        SELECT status
        FROM transactions
        WHERE trx_id=?
        """,
        (trx_id,)
    ).fetchone()

    if not row:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Transaction not found."
        }), 404

    if row["status"] != "pending":

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Only pending payment can be rejected."
        }), 400

    conn.execute(
        """
        UPDATE transactions
        SET status='rejected'
        WHERE trx_id=?
        """,
        (trx_id,)
    )

    conn.commit()
    conn.close()

    audit(
        "PAYMENT_REJECTED",
        trx_id
    )

    return jsonify({
        "success": True,
        "message":
        "❌ Payment rejected."
    })


# ============================================================
# ADMIN: WITHDRAWAL APPROVE
# ============================================================

@app.route(
    "/api/admin/approve-withdrawal",
    methods=["POST"]
)
@admin_required
def approve_withdrawal():

    data = request.get_json(
        silent=True
    ) or {}

    try:
        wid = int(data.get("id"))
    except Exception:

        return jsonify({
            "success": False,
            "message":
            "Invalid withdrawal ID."
        }), 400

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM withdrawals
        WHERE id=?
        """,
        (wid,)
    ).fetchone()

    if not row:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Withdrawal not found."
        }), 404

    if row["status"] != "pending":

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Withdrawal already processed."
        }), 400

    conn.execute(
        """
        UPDATE withdrawals
        SET status='approved'
        WHERE id=?
        """,
        (wid,)
    )

    conn.commit()
    conn.close()

    audit(
        "WITHDRAWAL_APPROVED",
        str(wid),
        f"user={row['user_id']}"
    )

    return jsonify({
        "success": True,
        "message":
        "✅ Withdrawal marked Paid & Approved."
    })


# ============================================================
# ADMIN: WITHDRAWAL REJECT + REFUND
# ============================================================

@app.route(
    "/api/admin/reject-withdrawal",
    methods=["POST"]
)
@admin_required
def reject_withdrawal():

    data = request.get_json(
        silent=True
    ) or {}

    try:
        wid = int(data.get("id"))
    except Exception:

        return jsonify({
            "success": False,
            "message":
            "Invalid ID."
        }), 400

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM withdrawals
        WHERE id=?
        """,
        (wid,)
    ).fetchone()

    if not row:

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Withdrawal not found."
        }), 404

    if row["status"] != "pending":

        conn.close()

        return jsonify({
            "success": False,
            "message":
            "Withdrawal already processed."
        }), 400

    conn.execute(
        """
        UPDATE withdrawals
        SET status='rejected'
        WHERE id=?
        """,
        (wid,)
    )

    conn.execute(
        """
        UPDATE users
        SET fomo_coins=fomo_coins+?
        WHERE user_id=?
        """,
        (
            row["fomo_spent"],
            row["user_id"]
        )
    )

    conn.execute(
        """
        INSERT INTO coin_ledger
        (user_id,amount,reason)
        VALUES (?,?,?)
        """,
        (
            row["user_id"],
            row["fomo_spent"],
            "Withdrawal rejected - FOMO refunded"
        )
    )

    conn.commit()
    conn.close()

    audit(
        "WITHDRAWAL_REJECTED_REFUNDED",
        str(wid),
        f"user={row['user_id']}"
    )

    return jsonify({
        "success": True,
        "message":
        "❌ Withdrawal rejected. FOMO refunded."
    })


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
@admin_required
def admin_panel():

    conn = db()

    users = conn.execute(
        """
        SELECT
            user_id,
            identifier,
            fomo_coins,
            subscription_expires_at,
            created_at
        FROM users
        ORDER BY created_at DESC
        """
    ).fetchall()

    transactions = conn.execute(
        """
        SELECT *
        FROM transactions
        ORDER BY created_at DESC
        """
    ).fetchall()

    withdrawals = conn.execute(
        """
        SELECT *
        FROM withdrawals
        ORDER BY created_at DESC
        """
    ).fetchall()

    logs = conn.execute(
        """
        SELECT *
        FROM admin_audit
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()

    total_users = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    pending_payments = conn.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE status='pending'
        """
    ).fetchone()[0]

    pending_withdrawals = conn.execute(
        """
        SELECT COUNT(*)
        FROM withdrawals
        WHERE status='pending'
        """
    ).fetchone()[0]

    total_coins = conn.execute(
        """
        SELECT COALESCE(SUM(fomo_coins),0)
        FROM users
        """
    ).fetchone()[0]

    conn.close()

    return render_template_string(
        ADMIN_HTML,
        users=users,
        transactions=transactions,
        withdrawals=withdrawals,
        logs=logs,
        total_users=total_users,
        pending_payments=pending_payments,
        pending_withdrawals=pending_withdrawals,
        total_coins=total_coins
    )


# ============================================================
# MAIN FRONTEND
# ============================================================

@app.route("/")
def index():

    return render_template_string(
        MAIN_HTML
    )


# ============================================================
# FRONTEND HTML
# ============================================================

MAIN_HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
name="viewport"
content="width=device-width,initial-scale=1.0">

<title>Skill Arena Hub</title>

<style>

:root{
    --bg:#07111f;
    --panel:#111c2e;
    --panel2:#172338;
    --border:#2d405b;
    --blue:#3b82f6;
    --cyan:#22d3ee;
    --green:#10b981;
    --red:#ef4444;
    --gold:#f59e0b;
    --purple:#8b5cf6;
    --pink:#ec4899;
    --text:#f8fafc;
    --muted:#94a3b8;
}

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:
        radial-gradient(
            circle at top right,
            rgba(59,130,246,.12),
            transparent 30%
        ),
        var(--bg);
    color:var(--text);
    font-family:
        Inter,
        Segoe UI,
        Arial,
        sans-serif;
}

button,
input,
select{
    font:inherit;
}

button{
    cursor:pointer;
}

.top-security{
    background:
        linear-gradient(
            90deg,
            #991b1b,
            #c2410c
        );
    padding:8px;
    text-align:center;
    font-size:12px;
    font-weight:700;
}

header{
    position:sticky;
    top:0;
    z-index:100;
    background:
        rgba(17,28,46,.95);
    backdrop-filter:blur(15px);
    border-bottom:1px solid var(--border);
    padding:14px 28px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:15px;
}

.logo{
    font-size:23px;
    font-weight:900;
    color:#60a5fa;
}

.stats{
    display:flex;
    gap:8px;
    align-items:center;
    flex-wrap:wrap;
}

.stat{
    background:#1e2c43;
    border:1px solid #334966;
    padding:7px 11px;
    border-radius:8px;
    font-size:12px;
}

.stat b{
    color:#fbbf24;
}

.container{
    width:min(1250px,94%);
    margin:30px auto 70px;
}

.hero{
    background:
        linear-gradient(
            135deg,
            rgba(59,130,246,.2),
            rgba(139,92,246,.16)
        );
    border:1px solid #35537d;
    border-radius:20px;
    padding:35px;
    margin-bottom:25px;
}

.hero h1{
    font-size:38px;
    margin:0 0 10px;
}

.hero p{
    color:#b6c4d8;
    max-width:700px;
    line-height:1.7;
}

.section-title{
    margin:35px 0 15px;
    border-left:4px solid var(--blue);
    padding-left:12px;
}

.grid{
    display:grid;
    grid-template-columns:
        repeat(auto-fit,minmax(250px,1fr));
    gap:18px;
}

.subscription-grid{
    grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    align-items:stretch;
}

.subscription-card{
    min-width:0;
    display:flex;
    flex-direction:column;
}

.subscription-card .btn{
    margin-top:auto;
}

.card{
    background:
        linear-gradient(
            145deg,
            #172338,
            #0f1929
        );
    border:1px solid var(--border);
    border-radius:16px;
    padding:22px;
    transition:.2s;
}

.card:hover{
    transform:translateY(-3px);
    border-color:#4b76ae;
}

.game-card{
    min-height:215px;
    display:flex;
    flex-direction:column;
    justify-content:center;
    align-items:center;
    text-align:center;
    cursor:pointer;
}

.game-card:hover{
    border-color:var(--green);
    box-shadow:
        0 10px 35px
        rgba(16,185,129,.13);
}

.game-icon{
    font-size:52px;
    margin-bottom:15px;
}

.game-name{
    font-size:19px;
    font-weight:800;
}

.game-desc{
    color:var(--muted);
    font-size:13px;
    margin-top:8px;
    line-height:1.5;
}

.btn{
    width:100%;
    border:0;
    padding:12px 16px;
    border-radius:9px;
    color:white;
    font-weight:800;
    background:var(--blue);
    transition:.2s;
}

.btn:hover{
    filter:brightness(1.12);
    transform:translateY(-1px);
}

.green{
    background:var(--green);
}

.red{
    background:var(--red);
}

.purple{
    background:var(--purple);
}

.gold{
    background:#d97706;
}

.pink{
    background:var(--pink);
}

.input,
.select{
    width:100%;
    padding:12px;
    border-radius:9px;
    border:1px solid #344861;
    background:#0a1423;
    color:white;
    margin:7px 0;
    outline:none;
}

.input:focus{
    border-color:var(--blue);
}

.offer{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:20px;
    flex-wrap:wrap;
}

.offer-info{
    flex:1;
}

.offer-info h3{
    margin:0 0 8px;
}

.offer-info p{
    color:#b7c5d7;
    line-height:1.6;
    font-size:14px;
}

.price{
    font-size:31px;
    font-weight:900;
    color:#34d399;
}

.badge{
    display:inline-block;
    padding:4px 9px;
    border-radius:999px;
    background:#24344e;
    color:#93c5fd;
    font-size:11px;
    font-weight:800;
}

.modal{
    display:none;
    position:fixed;
    inset:0;
    background:
        rgba(0,0,0,.88);
    z-index:1000;
    align-items:center;
    justify-content:center;
    padding:15px;
}

.modal-box{
    width:min(470px,96vw);
    max-height:94vh;
    overflow:auto;
    background:#111c2e;
    border:1px solid #344966;
    border-radius:18px;
    padding:26px;
    box-shadow:
        0 25px 80px
        rgba(0,0,0,.6);
}

.modal-title{
    margin-top:0;
    color:#60a5fa;
}

.close{
    margin-top:10px;
    background:#334155;
}

.game-modal{
    padding:0;
}

.game-screen{
    width:min(1000px,100vw);
    height:min(760px,100vh);
    background:#07111f;
    border:1px solid #2d405b;
    border-radius:16px;
    position:relative;
    overflow:hidden;
}

.game-head{
    height:62px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    padding:0 18px;
    background:#101b2d;
    border-bottom:1px solid #2d405b;
}

.game-body{
    height:calc(100% - 62px);
    display:flex;
    justify-content:center;
    align-items:center;
    overflow:auto;
    padding:20px;
}

.game-score{
    color:#fbbf24;
    font-weight:900;
}

.puzzle-board{
    width:330px;
    height:330px;
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:6px;
}

.puzzle-tile{
    border:0;
    border-radius:8px;
    background:#2563eb;
    color:white;
    font-size:28px;
    font-weight:900;
}

.puzzle-empty{
    background:#0b1626;
    border:1px dashed #344966;
}

.memory-grid{
    display:grid;
    grid-template-columns:repeat(5,60px);
    gap:9px;
}

.memory-card{
    width:60px;
    height:68px;
    border:0;
    border-radius:9px;
    background:#334155;
    color:white;
    font-size:27px;
    font-weight:900;
}

.memory-card.open{
    background:#8b5cf6;
}

.word-box{
    width:min(500px,95vw);
}

.riddle{
    background:#0b1626;
    border-left:4px solid var(--gold);
    padding:18px;
    border-radius:8px;
    line-height:1.6;
    margin:15px 0;
}

.word-grid{
    display:grid;
    grid-template-columns:
        repeat(8,42px);
    gap:4px;
    justify-content:center;
}

.word-cell{
    width:42px;
    height:42px;
    border:0;
    background:#1e3048;
    color:white;
    border-radius:6px;
    font-weight:900;
}

.word-cell.selected{
    background:#f97316;
}

.word-cell.found{
    background:#10b981;
}

.car-wrap{
    display:flex;
    flex-direction:column;
    align-items:center;
    gap:12px;
}

#carCanvas{
    background:#0f172a;
    border:3px solid #38bdf8;
    border-radius:10px;
    width:min(420px,86vw);
    height:auto;
    touch-action:none;
}

.car-controls{
    display:flex;
    gap:25px;
}

.car-control{
    width:100px;
    height:55px;
    border:2px solid #60a5fa;
    border-radius:12px;
    background:#1d4ed8;
    color:white;
    font-size:24px;
    font-weight:900;
    touch-action:manipulation;
}

.notice{
    padding:12px;
    border-radius:9px;
    background:#0b1626;
    border:1px solid #2d405b;
    color:#aebed1;
    font-size:13px;
    line-height:1.6;
}

.ref-box{
    background:#0a1423;
    border:1px solid #344861;
    padding:10px;
    border-radius:8px;
    word-break:break-all;
    color:#93c5fd;
    font-size:12px;
}

.footer{
    text-align:center;
    color:#64748b;
    padding:30px;
    font-size:12px;
}

/* Small visual polish — keeps the existing layout and controls intact. */
.game-card,
.subscription-card,
.stat{
    transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease;
}

.game-card:hover,
.subscription-card:hover{
    transform:translateY(-3px);
    box-shadow:0 14px 35px rgba(0,0,0,.22);
}

.demo-badge{
    display:inline-flex;
    align-items:center;
    gap:6px;
    padding:5px 10px;
    border-radius:999px;
    background:rgba(139,92,246,.16);
    border:1px solid rgba(167,139,250,.45);
    color:#c4b5fd;
    font-size:11px;
    font-weight:900;
    letter-spacing:.3px;
}

.demo-notice{
    margin-top:14px;
    border:1px solid rgba(139,92,246,.35);
    background:linear-gradient(135deg,rgba(76,29,149,.20),rgba(30,41,59,.55));
}

@media(max-width:700px){

    header{
        position:relative;
        padding:12px;
    }

    .logo{
        font-size:18px;
    }

    .stats{
        justify-content:flex-end;
    }

    .hero{
        padding:22px;
    }

    .hero h1{
        font-size:28px;
    }

    .puzzle-board{
        width:290px;
        height:290px;
    }

    .memory-grid{
        grid-template-columns:
            repeat(4,60px);
    }

    .memory-card{
        width:60px;
        height:70px;
    }

    .word-grid{
        grid-template-columns:
            repeat(8,35px);
    }

    .word-cell{
        width:35px;
        height:35px;
        font-size:12px;
    }

}

</style>

</head>

<body>

<div class="top-security">
🔒 Single Device Security — Ek waqt mein account sirf ek device par active rahega.
</div>


<header>

<div class="logo">
⚡ Skill Arena Hub
</div>

<div class="stats">

<div class="stat">
ID:
<b id="uid">Not Logged In</b>
</div>

<div class="stat">
⏱️
<b id="time">No Pass</b>
</div>

<div class="stat">
💎
<b id="coins">0</b>
FOMO
</div>

<button
id="sellButton"
class="btn green"
style="width:auto"
onclick="openWithdraw()">
💸 Sell
</button>

</div>

</header>


<div class="container">


<div class="hero">

<h1>
🏆 Skill Arena
</h1>

<p>
Play skill-based games, complete challenges,
earn FOMO Coins and use your coins for rewards.
Paid users ke game results active Pass ke andar process hote hain. Demo Mode mein games free hain, lekin FOMO rewards disabled hain.
</p>

<div
style="
display:flex;
gap:10px;
flex-wrap:wrap;
margin-top:18px;
">

<button
class="btn"
style="width:auto"
onclick="openAuth()">
🔐 Login / Register
</button>

<button
id="referralButton"
class="btn purple"
style="width:auto"
onclick="openReferral()">
🎁 Invite & Earn
</button>

</div>

<div class="notice demo-notice">
🎮 <b>Demo Mode</b> — games free play karein. FOMO earning, rewards aur FOMO selling disabled hain.
</div>

</div>


<!-- SUBSCRIPTIONS -->

<h2 class="section-title">
💳 Subscription Passes
</h2>

<div class="grid subscription-grid">


<div class="card subscription-card">

<span class="badge">
1 HOUR
</span>

<h3>Starter Pass</h3>

<div class="price">
PKR 50
</div>

<p class="game-desc">
1 hour game access.
</p>

<button
class="btn"
onclick="openPayment(50,1,0)">
Buy 1 Hour
</button>

</div>


<div class="card subscription-card">

<span class="badge">
2 HOURS
</span>

<h3>Pro Pass</h3>

<div class="price">
PKR 100
</div>

<p class="game-desc">
2 hours game access.
</p>

<button
class="btn"
onclick="openPayment(100,2,0)">
Buy 2 Hours
</button>

</div>


<div class="card subscription-card">

<span class="badge">
3 HOURS
</span>

<h3>Elite Pass</h3>

<div class="price">
PKR 180
</div>

<p class="game-desc">
3 hours game access.
</p>

<button
class="btn"
onclick="openPayment(180,3,0)">
Buy 3 Hours
</button>

</div>


<div class="card subscription-card">

<span class="badge">
FOMO
</span>

<h3>1 Hour FOMO Pass</h3>

<div class="price">
500 FOMO
</div>

<p class="game-desc">
500 FOMO Coins = 1 Hour Pass.
</p>

<button
class="btn purple"
onclick="redeemPass(500)">
Redeem 500 FOMO
</button>

</div>


<div class="card subscription-card">

<span class="badge">
FOMO
</span>

<h3>2 Hour FOMO Pass</h3>

<div class="price">
1000 FOMO
</div>

<p class="game-desc">
1000 FOMO Coins = 2 Hour Pass.
</p>

<button
class="btn purple"
onclick="redeemPass(1000)">
Redeem 1000 FOMO
</button>

</div>


<div class="card subscription-card">

<span class="badge">
FOMO
</span>

<h3>3 Hour FOMO Pass</h3>

<div class="price">
1500 FOMO
</div>

<p class="game-desc">
1500 FOMO Coins = 3 Hour Pass.
</p>

<button
class="btn purple"
onclick="redeemPass(1500)">
Redeem 1500 FOMO
</button>

</div>


<div class="card subscription-card">

<span class="badge">
FOMO
</span>

<h3>4 Hour FOMO Pass</h3>

<div class="price">
2000 FOMO
</div>

<p class="game-desc">
2000 FOMO Coins = 4 Hour Pass.
</p>

<button
class="btn purple"
onclick="redeemPass(2000)">
Redeem 2000 FOMO
</button>

</div>

</div>


<!-- SOCIAL -->

<h2 class="section-title">
🎁 Rewards
</h2>


<div class="card offer">

<div class="offer-info">

<h3>
📸 Skill Arena Support & Instagram
</h3>

<p>
Instagram follow offer.
One-time reward:
<b>10 FOMO Coins</b>.
</p>

</div>

<div style="width:240px">

<a
href="https://instagram.com/"
target="_blank"
class="btn pink"
style="
display:block;
text-align:center;
text-decoration:none;
margin-bottom:8px;
">
📷 Open Instagram
</a>

<button
id="instaClaim"
class="btn green"
onclick="claimInstagram()">
Claim 10 FOMO
</button>

</div>

</div>


<div
class="card"
style="
margin-top:18px;
border-color:#4f46e5;
">

<h3>
🎁 Invite & Earn
</h3>

<p class="game-desc">
Jab invited user ki first subscription approve hogi:
dono users ko 25 FOMO Coins.
</p>

<div
class="ref-box"
id="refLink">
Login karke referral link dekhein.
</div>

<button
class="btn purple"
style="margin-top:10px"
onclick="copyReferral()">
Copy Referral Link
</button>

</div>


<!-- GAMES -->

<h2 class="section-title">
🎮 Game Zone
</h2>

<div class="grid">


<div
class="card game-card"
onclick="startPuzzle()">

<div class="game-icon">
🧩
</div>

<div class="game-name">
Slide Puzzle
</div>

<div class="game-desc">
3×3 puzzle.
Hard shuffle.
Complete to WIN.
</div>

</div>


<div
class="card game-card"
onclick="startMemory()">

<div class="game-icon">
🧠
</div>

<div class="game-name">
Memory Matching
</div>

<div class="game-desc">
Match all cards before move limit.
</div>

</div>


<div
class="card game-card"
onclick="startWordEscape()">

<div class="game-icon">
🚪
</div>

<div class="game-name">
Word Escape
</div>

<div class="game-desc">
Solve multiple riddles before timer ends.
</div>

</div>


<div
class="card game-card"
onclick="startWordSearch()">

<div class="game-icon">
🔠
</div>

<div class="game-name">
Word Search
</div>

<div class="game-desc">
Find all hidden words.
</div>

</div>


<div
class="card game-card"
style="
border-color:#0ea5e9;
"
onclick="startCarEscape()">

<div class="game-icon">
🚗
</div>

<div class="game-name">
Car Escape — 4 Lanes
</div>

<div
class="game-desc"
style="color:#38bdf8">
Avoid traffic across 4 lanes until
<b>1000 SCORE</b>.
</div>

</div>


</div>


<div class="card" style="margin-top:25px">

<h3>
💎 FOMO Coin Rules
</h3>

<div class="notice">

🏆 Game WIN:
<b style="color:#10b981">
+15 FOMO
</b>
<br><br>

💥 Game LOSE:
<b style="color:#ef4444">
-5 FOMO
</b>
<br><br>

💰 Withdrawal:
100 FOMO minimum.
<br><br>

⏰ Withdrawal sirf active Pass ke andar.

</div>

</div>


</div>


<div class="footer">
Skill Arena Hub • Skill Gaming Platform
</div>


<!-- ========================================================
AUTH MODAL
======================================================== -->

<div
id="authModal"
class="modal">

<div class="modal-box">

<h2
class="modal-title"
id="authTitle">
🔐 Create Account
</h2>

<p
class="game-desc">
New account par 20 FOMO Coins welcome bonus.
</p>

<input
id="authIdentifier"
class="input"
placeholder="Gmail or Mobile Number">

<input
id="authPassword"
class="input"
type="password"
placeholder="Password">

<button
id="authButton"
class="btn"
onclick="submitAuth()">
Register
</button>

<button
class="btn purple"
style="margin-top:8px"
onclick="startDemo()">
🎮 Play Free Demo
</button>

<p class="game-desc" style="text-align:center;margin-top:8px">
Demo Mode: games playable, FOMO rewards disabled.
</p>

<button
class="btn purple"
style="margin-top:8px"
onclick="openForgot()">
🔑 Forgot Password
</button>

<p
id="authSwitch"
style="
text-align:center;
color:#60a5fa;
cursor:pointer;
margin-top:15px;
"
onclick="toggleAuth()">
Already have account? Login
</p>

<button
class="btn close"
onclick="closeModal('authModal')">
Close
</button>

</div>

</div>


<!-- ========================================================
FORGOT PASSWORD
======================================================== -->

<div
id="forgotModal"
class="modal">

<div class="modal-box">

<h2 class="modal-title">
🔑 Forgot Password
</h2>

<p class="game-desc">
Registered Gmail/Mobile enter karein.
</p>

<input
id="resetIdentifier"
class="input"
placeholder="Gmail or Mobile Number">

<button
class="btn purple"
onclick="requestReset()">
Generate Reset Code
</button>

<div
id="resetStep"
style="display:none">

<p
class="notice"
style="
margin-top:15px;
color:#fbbf24;
">
Reset code 10 minutes valid hai.
</p>

<input
id="resetCode"
class="input"
maxlength="6"
placeholder="6 Digit Reset Code">

<input
id="newPassword"
class="input"
type="password"
placeholder="New Password">

<input
id="confirmPassword"
class="input"
type="password"
placeholder="Confirm Password">

<button
class="btn green"
onclick="changePassword()">
🔐 Change Password
</button>

</div>

<button
class="btn close"
onclick="closeModal('forgotModal')">
Close
</button>

</div>

</div>


<!-- ========================================================
PAYMENT
======================================================== -->

<div
id="paymentModal"
class="modal">

<div class="modal-box">

<h2 class="modal-title">
💳 Manual Payment
</h2>

<div class="notice">
<b>NayaPay Payment</b><br><br>
Account / Wallet: <b>NayaPay</b><br>
Account Name: <b>Muhammad Raffy Umer</b><br>
Account Number: <b>03250150477</b><br><br>
Payment apne NayaPay wallet se transfer karein. Transfer ke baad apni <b>TRX ID</b> neeche enter karke submit karein. Admin payment verify karke Pass approve/reject karega.
</div>

<p
id="paymentDetails"
class="notice">
</p>

<input
id="paymentTrxId"
class="input"
type="text"
placeholder="Enter NayaPay TRX ID"
autocomplete="off">

<button
class="btn green"
onclick="submitPayment()">
📨 Submit TRX ID
</button>

<button
class="btn close"
onclick="closeModal('paymentModal')">
Close
</button>

</div>

</div>


<!-- ========================================================
WITHDRAW
======================================================== -->

<div
id="withdrawModal"
class="modal">

<div class="modal-box">

<h2 class="modal-title">
💸 Sell FOMO Coins
</h2>

<div class="notice">
Conversion:
<b>1 FOMO = PKR 0.04</b>
<br>
Minimum:
<b>100 FOMO</b>
<br>
Active Pass required.
</div>

<select
id="withdrawMethod"
class="select">

<option value="NayaPay">
NayaPay
</option>

<option value="JazzCash">
JazzCash
</option>

<option value="Easypaisa">
Easypaisa
</option>

<option value="Raast">
Raast
</option>

</select>

<input
id="withdrawTitle"
class="input"
placeholder="Account Title">

<input
id="withdrawAccount"
class="input"
placeholder="Account Number">

<input
id="withdrawCoins"
class="input"
type="number"
min="100"
placeholder="FOMO Coins">

<button
class="btn green"
onclick="submitWithdrawal()">
Submit Withdrawal
</button>

<button
class="btn close"
onclick="closeModal('withdrawModal')">
Close
</button>

</div>

</div>


<!-- ========================================================
REFERRAL
======================================================== -->

<div
id="referralModal"
class="modal">

<div class="modal-box">

<h2 class="modal-title">
🎁 Invite & Earn
</h2>

<p class="game-desc">
Apna referral link friends ko share karein.
</p>

<div
id="referralFull"
class="ref-box">
</div>

<button
class="btn purple"
style="margin-top:10px"
onclick="copyReferral()">
Copy Link
</button>

<button
class="btn close"
onclick="closeModal('referralModal')">
Close
</button>

</div>

</div>


<!-- ========================================================
PUZZLE GAME
======================================================== -->

<div
id="puzzleModal"
class="modal game-modal">

<div class="game-screen">

<div class="game-head">

<b>🧩 Slide Puzzle</b>

<div>
Score:
<span
id="puzzleScore"
class="game-score">
0
</span>

<span
id="puzzleTimer"
class="game-score"
style="margin-left:12px">
65s
</span>

<button
class="btn red"
style="width:auto;margin-left:10px"
onclick="quitGame('puzzle')">
Quit
</button>

</div>

</div>

<div class="game-body">

<div>

<div
id="puzzleBoard"
class="puzzle-board">
</div>

<div style="display:grid;grid-template-columns:repeat(3,52px);gap:8px;justify-content:center;margin:14px auto 8px">
<div></div>
<button type="button" class="btn" style="width:52px;height:44px;padding:0" onclick="movePuzzleDirection('up')" aria-label="Move up">⬆️</button>
<div></div>
<button type="button" class="btn" style="width:52px;height:44px;padding:0" onclick="movePuzzleDirection('left')" aria-label="Move left">⬅️</button>
<button type="button" class="btn" style="width:52px;height:44px;padding:0" onclick="movePuzzleDirection('down')" aria-label="Move down">⬇️</button>
<button type="button" class="btn" style="width:52px;height:44px;padding:0" onclick="movePuzzleDirection('right')" aria-label="Move right">➡️</button>
</div>

<p
class="game-desc"
style="text-align:center">
Arrange 1–8 in correct order.
</p>

</div>

</div>

</div>

</div>


<!-- ========================================================
MEMORY
======================================================== -->

<div
id="memoryModal"
class="modal game-modal">

<div class="game-screen">

<div class="game-head">

<b>🧠 Memory Matching</b>

<div>

Moves:
<span
id="memoryMoves"
class="game-score">
0
</span>

<button
class="btn red"
style="width:auto;margin-left:10px"
onclick="quitGame('memory')">
Quit
</button>

</div>

</div>

<div class="game-body">

<div>

<div
id="memoryGrid"
class="memory-grid">
</div>

<p
class="game-desc"
style="text-align:center">
Match every pair.
Maximum 38 moves.
</p>

</div>

</div>

</div>

</div>


<!-- ========================================================
WORD ESCAPE
======================================================== -->

<div
id="wordEscapeModal"
class="modal game-modal">

<div class="game-screen">

<div class="game-head">

<b>🚪 Word Escape</b>

<div>

Level:
<span
id="weLevel"
class="game-score">
1
</span>

<button
class="btn red"
style="width:auto;margin-left:10px"
onclick="quitGame('word_escape')">
Quit
</button>

</div>

</div>

<div class="game-body">

<div class="word-box">

<div
id="weTimer"
class="notice">
20 seconds
</div>

<div
id="weRiddle"
class="riddle">
</div>

<input
id="weAnswer"
class="input"
placeholder="Your answer">

<button
class="btn purple"
onclick="showRiddleHint()">
💡 Hint
</button>

<button
class="btn"
onclick="checkRiddle()">
Unlock Door
</button>

<p
id="weMessage"
style="text-align:center">
</p>

</div>

</div>

</div>

</div>


<!-- ========================================================
WORD SEARCH
======================================================== -->

<div
id="wordSearchModal"
class="modal game-modal">

<div class="game-screen">

<div class="game-head">

<b>🔠 Word Search</b>

<div>

Found:
<span
id="wsFound"
class="game-score">
0
</span>/10

<span
id="wsTimer"
class="game-score"
style="margin-left:12px">
75s
</span>

<button
class="btn red"
style="width:auto;margin-left:10px"
onclick="quitGame('word_search')">
Quit
</button>

</div>

</div>

<div class="game-body">

<div>

<div
id="wsWords"
style="
text-align:center;
margin-bottom:15px;
color:#94a3b8;
">
</div>

<div
id="wsGrid"
class="word-grid">
</div>

<p
class="game-desc"
style="text-align:center">
Click letters in sequence to find words.
</p>

</div>

</div>

</div>

</div>


<!-- ========================================================
CAR ESCAPE
======================================================== -->

<div
id="carModal"
class="modal game-modal">

<div class="game-screen">

<div class="game-head">

<b style="color:#38bdf8">
🚗 CAR ESCAPE — HARD MODE
</b>

<div>

Score:
<span
id="carScore"
class="game-score">
0
</span>
/
1000

<button
class="btn red"
style="width:auto;margin-left:10px"
onclick="quitCar()">
Quit
</button>

</div>

</div>

<div class="game-body">

<div class="car-wrap">

<canvas
id="carCanvas"
width="420"
height="650">
</canvas>

<div
class="car-controls">

<button
class="car-control"
onpointerdown="moveCar(-1)">
⬅️
</button>

<button
class="car-control"
onpointerdown="moveCar(1)">
➡️
</button>

</div>

<div class="notice">
🚗 Target: <b>1000 Score</b><br>
🛣️ 4 lanes • Easier traffic<br>
💥 Crash before 1000 = Lose<br>
🏆 1000 = Win (1 minute se zyada gameplay)<br>
⚡ Traffic speed continuously increases.
</div>

</div>

</div>

</div>

</div>


<script>

/* ============================================================
GLOBAL
============================================================ */

const API = "";

let userId =
    localStorage.getItem(
        "sa_user_id"
    );

let deviceToken =
    localStorage.getItem(
        "sa_device_token"
    );

let isLogin = false;
let demoMode = localStorage.getItem("sa_demo_mode") === "1";

let currentPayment = null;

let activeGameToken = null;


/* ============================================================
GENERAL
============================================================ */

function closeModal(id){

    const el =
        document.getElementById(id);

    if(el){
        el.style.display="none";
    }

}


function openModal(id){

    document.getElementById(id)
        .style.display="flex";

}


function api(url, options={}){

    return fetch(
        API + url,
        options
    ).then(
        r => r.json()
    );

}


/* ============================================================
AUTH
============================================================ */

function openAuth(){

    openModal("authModal");

}


function toggleAuth(){

    isLogin = !isLogin;

    document.getElementById(
        "authTitle"
    ).innerText =
        isLogin
        ? "🔐 Login"
        : "🔐 Create Account";

    document.getElementById(
        "authButton"
    ).innerText =
        isLogin
        ? "Login"
        : "Register";

    document.getElementById(
        "authSwitch"
    ).innerText =
        isLogin
        ? "New user? Create account"
        : "Already have account? Login";

}


async function submitAuth(){

    const identifier =
        document.getElementById(
            "authIdentifier"
        ).value.trim();

    const password =
        document.getElementById(
            "authPassword"
        ).value;

    if(!identifier || !password){

        alert(
            "❌ Email/Mobile aur password enter karein."
        );

        return;
    }

    const endpoint =
        isLogin
        ? "/api/login"
        : "/api/register";

    const data =
        await api(
            endpoint,
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    identifier,
                    password
                })
            }
        );

    alert(
        data.message
    );

    if(data.success){

        userId =
            data.user_id;

        deviceToken =
            data.device_token;

        demoMode = false;
        localStorage.removeItem("sa_demo_mode");

        localStorage.setItem(
            "sa_user_id",
            userId
        );

        localStorage.setItem(
            "sa_device_token",
            deviceToken
        );

        closeModal(
            "authModal"
        );

        refreshUser();

    }

}


/* ============================================================
FORGOT PASSWORD
============================================================ */

async function startDemo(){

    const data = await api("/api/demo-login", {
        method:"POST",
        headers:{"Content-Type":"application/json"}
    });

    alert(data.message);

    if(data.success){
        userId=data.user_id;
        deviceToken=data.device_token;
        demoMode=true;
        localStorage.removeItem("sa_demo_mode");
        localStorage.setItem("sa_user_id", userId);
        localStorage.setItem("sa_device_token", deviceToken);
        localStorage.setItem("sa_demo_mode", "1");
        closeModal("authModal");
        refreshUser();
    }
}


function openForgot(){

    closeModal(
        "authModal"
    );

    document.getElementById(
        "resetStep"
    ).style.display="none";

    openModal(
        "forgotModal"
    );

}


async function requestReset(){

    const identifier =
        document.getElementById(
            "resetIdentifier"
        ).value.trim();

    if(!identifier){

        alert(
            "❌ Email/Mobile enter karein."
        );

        return;
    }

    const data =
        await api(
            "/api/request-password-reset",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    identifier
                })
            }
        );

    alert(
        data.reset_code
        ? data.message
          + "\n\nRESET CODE: "
          + data.reset_code
        : data.message
    );

    if(data.success){

        document.getElementById(
            "resetStep"
        ).style.display="block";

    }

}


async function changePassword(){

    const identifier =
        document.getElementById(
            "resetIdentifier"
        ).value.trim();

    const code =
        document.getElementById(
            "resetCode"
        ).value.trim();

    const password =
        document.getElementById(
            "newPassword"
        ).value;

    const confirm =
        document.getElementById(
            "confirmPassword"
        ).value;

    if(password !== confirm){

        alert(
            "❌ Passwords match nahi kar rahe."
        );

        return;
    }

    const data =
        await api(
            "/api/reset-password",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    identifier,
                    reset_code:code,
                    new_password:password
                })
            }
        );

    alert(
        data.message
    );

    if(data.success){

        closeModal(
            "forgotModal"
        );

        isLogin=true;

        openAuth();

    }

}


/* ============================================================
USER
============================================================ */

async function refreshUser(){

    if(!userId) return;

    const data =
        await api(
            "/api/user-data/"
            + encodeURIComponent(userId)
        );

    if(!data.success){

        logout();

        return;
    }

    document.getElementById(
        "uid"
    ).innerText =
        userId;

    document.getElementById(
        "coins"
    ).innerText =
        data.fomo_coins;

    updateTimer(
        data.active_seconds
    );

    if(data.demo_mode){
        document.getElementById("time").innerText = "Demo Mode";
    }

    const sellButton = document.getElementById("sellButton");
    if(sellButton){
        sellButton.disabled = !!data.demo_mode;
        sellButton.style.opacity = data.demo_mode ? ".55" : "1";
    }

    const referralButton = document.getElementById("referralButton");
    if(referralButton){
        referralButton.disabled = !!data.demo_mode;
        referralButton.style.opacity = data.demo_mode ? ".55" : "1";
    }

    document.getElementById(
        "instaClaim"
    ).disabled =
        !!data.instagram_followed || !!data.demo_mode;

    const link =
        location.origin
        + "/?ref="
        + encodeURIComponent(
            userId
        );

    document.getElementById(
        "refLink"
    ).innerText =
        link;

}


function updateTimer(seconds){

    if(seconds <= 0){

        document.getElementById(
            "time"
        ).innerText =
            "No Pass";

        return;
    }

    const h =
        Math.floor(
            seconds / 3600
        );

    const m =
        Math.floor(
            (seconds % 3600)
            / 60
        );

    const s =
        seconds % 60;

    document.getElementById(
        "time"
    ).innerText =
        h + "h "
        + m + "m "
        + s + "s";

    setTimeout(
        ()=>{
            updateTimer(
                seconds - 1
            )
        },
        1000
    );

}


function logout(){

    localStorage.removeItem(
        "sa_user_id"
    );

    localStorage.removeItem(
        "sa_device_token"
    );

    userId=null;
    deviceToken=null;

    document.getElementById(
        "uid"
    ).innerText =
        "Not Logged In";

    document.getElementById(
        "coins"
    ).innerText =
        "0";

    document.getElementById(
        "time"
    ).innerText =
        "No Pass";

}


/* ============================================================
SESSION CHECK
============================================================ */

async function checkSession(){

    if(!userId || !deviceToken)
        return;

    const data =
        await api(
            "/api/verify-session",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    user_id:userId,
                    device_token:deviceToken,
                    coins:coins
                })
            }
        );

    if(!data.valid){

        alert(
            data.message
            || "Session expired."
        );

        logout();

    }

}


/* ============================================================
PAYMENT
============================================================ */

function openPayment(
    price,
    hours,
    reward
){

    if(!userId){

        alert(
            "Pehle login karein."
        );

        openAuth();

        return;
    }

    currentPayment={
        price,
        hours,
        reward
    };

    const trxInput = document.getElementById("paymentTrxId");
    if(trxInput) trxInput.value = "";

    document.getElementById(
        "paymentDetails"
    ).innerHTML =
        "You selected: <b>"
        + hours
        + " Hour Pass</b><br>"
        + "Amount: <b>PKR "
        + price
        + "</b>";

    openModal(
        "paymentModal"
    );

}


async function submitPayment(){

    if(!currentPayment) return;

    const trxInput = document.getElementById("paymentTrxId");
    const trxId = (trxInput?.value || "").trim();

    if(!trxId){
        alert("❌ Pehle NayaPay TRX ID enter karein.");
        trxInput?.focus();
        return;
    }

    const data = await api(
        "/api/create-payment",
        {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({
                user_id:userId,
                device_token:deviceToken,
                price:currentPayment.price,
                hours:currentPayment.hours,
                trx_id:trxId
            })
        }
    );

    alert(data.message || "Payment request submit ho gayi.");

    if(!data.success) return;

    if(trxInput) trxInput.value = "";
    closeModal("paymentModal");
}



/* ============================================================
REDEEM
============================================================ */

async function redeemPass(coins=500){

    if(!userId){

        openAuth();

        return;
    }

    const data =
        await api(
            "/api/redeem-pass",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    user_id:userId,
                    device_token:deviceToken,
                    coins:coins
                })
            }
        );

    alert(
        data.message
    );

    if(data.success){

        refreshUser();

    }

}


/* ============================================================
INSTAGRAM
============================================================ */

async function claimInstagram(){

    if(!userId){

        openAuth();

        return;
    }

    const data =
        await api(
            "/api/claim-instagram-reward",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    user_id:userId,
                    device_token:deviceToken
                })
            }
        );

    alert(
        data.message
    );

    refreshUser();

}


/* ============================================================
WITHDRAW
============================================================ */

function openWithdraw(){

    if(!userId){

        openAuth();

        return;
    }

    openModal(
        "withdrawModal"
    );

}


async function submitWithdrawal(){

    const method =
        document.getElementById(
            "withdrawMethod"
        ).value;

    const title =
        document.getElementById(
            "withdrawTitle"
        ).value.trim();

    const account =
        document.getElementById(
            "withdrawAccount"
        ).value.trim();

    const coins =
        Number(
            document.getElementById(
                "withdrawCoins"
            ).value
        );

    if(!title || !account || !coins){

        alert(
            "❌ Complete details enter karein."
        );

        return;
    }

    const data =
        await api(
            "/api/submit-withdrawal",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    user_id:userId,
                    device_token:deviceToken,
                    method,
                    title,
                    acc:account,
                    fomo_coins:coins
                })
            }
        );

    alert(
        data.message
    );

    if(data.success){

        closeModal(
            "withdrawModal"
        );

        refreshUser();

    }

}


/* ============================================================
REFERRAL
============================================================ */

function referralLink(){

    if(!userId)
        return "";

    return (
        location.origin
        + "/?ref="
        + encodeURIComponent(
            userId
        )
    );

}


function openReferral(){

    if(!userId){

        openAuth();

        return;
    }

    document.getElementById(
        "referralFull"
    ).innerText =
        referralLink();

    openModal(
        "referralModal"
    );

}


function copyReferral(){

    const link =
        referralLink();

    if(!link){

        alert(
            "Pehle login karein."
        );

        return;
    }

    navigator.clipboard
        .writeText(link)
        .then(
            ()=>{
                alert(
                    "✅ Referral link copied."
                );
            }
        );

}


/* ============================================================
GAME START SECURITY
============================================================ */

async function startServerGame(
    game
){

    if(!userId){

        openAuth();

        return false;
    }

    const data =
        await api(
            "/api/game/start",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    user_id:userId,
                    device_token:deviceToken,
                    game
                })
            }
        );

    if(!data.success){

        alert(
            data.message
        );

        return false;
    }

    activeGameToken =
        data.game_token;

    return true;

}


async function finishServerGame(
    game,
    result
){

    if(!activeGameToken)
        return;

    const token =
        activeGameToken;

    activeGameToken=null;

    const data =
        await api(
            "/api/game/result",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    user_id:userId,
                    device_token:deviceToken,
                    game,
                    game_token:token,
                    result
                })
            }
        );

    if(data.success){

        alert(
            demoMode
            ? (result==="win"
                ? "🏆 YOU WIN!\n🎮 Demo Mode — FOMO reward disabled"
                : "💥 YOU LOSE!\n🎮 Demo Mode — no FOMO change")
            : (result==="win"
                ? "🏆 YOU WIN!\n+15 FOMO Coins"
                : "💥 YOU LOSE!\n-5 FOMO Coins")
        );

        refreshUser();

    }else{

        alert(
            data.message
        );

    }

}


/* ============================================================
PUZZLE
============================================================ */

let puzzle=[];
let puzzleMoves=0;
let puzzleRunning=false;
let puzzleTimer=65;
let puzzleInterval=null;


async function startPuzzle(){

    if(
        !(await startServerGame(
            "slide"
        ))
    ) return;

    puzzleRunning=true;

    puzzleMoves=0;
    clearInterval(puzzleInterval);
    puzzleTimer=65;
    document.getElementById("puzzleTimer").innerText="65s";

    puzzle=[
        1,2,3,
        4,5,6,
        7,8,0
    ];

    // Hard shuffle
    for(let i=0;i<120;i++){

        const empty =
            puzzle.indexOf(0);

        const row =
            Math.floor(
                empty/3
            );

        const col =
            empty%3;

        const possible=[];

        if(row>0)
            possible.push(
                empty-3
            );

        if(row<2)
            possible.push(
                empty+3
            );

        if(col>0)
            possible.push(
                empty-1
            );

        if(col<2)
            possible.push(
                empty+1
            );

        const pick =
            possible[
                Math.floor(
                    Math.random()
                    * possible.length
                )
            ];

        [
            puzzle[empty],
            puzzle[pick]
        ]=[
            puzzle[pick],
            puzzle[empty]
        ];

    }

    renderPuzzle();

    openModal(
        "puzzleModal"
    );

    puzzleInterval=setInterval(()=>{
        if(!puzzleRunning) return;
        puzzleTimer--;
        document.getElementById("puzzleTimer").innerText=puzzleTimer+"s";
        if(puzzleTimer<=0){
            clearInterval(puzzleInterval);
            puzzleRunning=false;
            closeModal("puzzleModal");
            finishServerGame("slide","lose");
        }
    },1000);

}


function renderPuzzle(){

    const board =
        document.getElementById(
            "puzzleBoard"
        );

    board.innerHTML="";

    puzzle.forEach(
        (n,index)=>{

            const b =
                document.createElement(
                    "button"
                );

            b.className =
                "puzzle-tile";

            if(n===0){

                b.classList.add(
                    "puzzle-empty"
                );

                b.innerText="";

            }else{

                b.innerText=n;

                b.onclick=()=>{
                    movePuzzle(index)
                };

            }

            board.appendChild(b);

        }
    );

    document.getElementById(
        "puzzleScore"
    ).innerText=
        puzzleMoves;

}


function movePuzzleDirection(direction){

    if(!puzzleRunning)
        return;

    const empty = puzzle.indexOf(0);
    const row = Math.floor(empty/3);
    const col = empty%3;
    let target = -1;

    if(direction === "up" && row < 2) target = empty + 3;
    if(direction === "down" && row > 0) target = empty - 3;
    if(direction === "left" && col < 2) target = empty + 1;
    if(direction === "right" && col > 0) target = empty - 1;

    if(target >= 0)
        movePuzzle(target);
}


function movePuzzle(index){

    if(!puzzleRunning)
        return;

    const empty =
        puzzle.indexOf(0);

    const diff =
        Math.abs(
            index-empty
        );

    if(
        !(
            diff===1
            || diff===3
        )
    ) return;

    // prevent row wrap
    if(
        diff===1
        &&
        Math.floor(index/3)
        !==
        Math.floor(empty/3)
    ){
        return;
    }

    [
        puzzle[index],
        puzzle[empty]
    ]=[
        puzzle[empty],
        puzzle[index]
    ];

    puzzleMoves++;

    renderPuzzle();

    const solved =
        puzzle.join(",")
        ===
        "1,2,3,4,5,6,7,8,0";

    if(solved){

        puzzleRunning=false;

        closeModal(
            "puzzleModal"
        );

        finishServerGame(
            "slide",
            "win"
        );

    }

}


document.addEventListener(
    "keydown",
    e=>{
        if(!puzzleRunning) return;

        const keys = {
            ArrowUp: "up",
            ArrowDown: "down",
            ArrowLeft: "left",
            ArrowRight: "right",
            w: "up", W: "up",
            s: "down", S: "down",
            a: "left", A: "left",
            d: "right", D: "right"
        };

        const direction = keys[e.key];
        if(direction){
            e.preventDefault();
            movePuzzleDirection(direction);
        }
    }
);


/* ============================================================
MEMORY
============================================================ */

let memoryValues=[];
let memoryOpen=[];
let memoryMatches=0;
let memoryMoves=0;
let memoryBusy=false;
let memoryRunning=false;


async function startMemory(){

    if(
        !(await startServerGame(
            "memory"
        ))
    ) return;

    memoryRunning=true;
    memoryMatches=0;
    memoryMoves=0;
    memoryBusy=false;
    memoryOpen=[];

    const symbols=[
        "🍎","🍎","🍋","🍋","🍇","🍇","🍉","🍉","🍒","🍒",
        "🥝","🥝","🍓","🍓","🍊","🍊","🍌","🍌","🍍","🍍",
        "🥭","🥭","🍑","🍑","🍐","🍐","🥥","🥥","🍈","🍈"
    ];

    memoryValues =
        symbols.sort(
            ()=>Math.random()-.5
        );

    renderMemory();

    openModal(
        "memoryModal"
    );

    // Show the shuffled board for 3 seconds before covering the cards.
    memoryOpen=[...Array(memoryValues.length).keys()];
    renderMemory();
    setTimeout(()=>{
        if(memoryRunning){
            memoryOpen=[];
            renderMemory();
        }
    },3000);

}


function renderMemory(){

    const grid =
        document.getElementById(
            "memoryGrid"
        );

    grid.innerHTML="";

    memoryValues.forEach(
        (value,index)=>{

            const b =
                document.createElement(
                    "button"
                );

            b.className =
                "memory-card";

            const open =
                memoryOpen.includes(
                    index
                );

            if(open){

                b.classList.add(
                    "open"
                );

                b.innerText=value;

            }else{

                b.innerText="❓";

            }

            b.onclick=()=>{
                memoryClick(
                    index
                )
            };

            grid.appendChild(b);

        }
    );

    document.getElementById(
        "memoryMoves"
    ).innerText=
        memoryMoves;

}


async function memoryClick(index){

    if(
        !memoryRunning
        ||
        memoryBusy
        ||
        memoryOpen.includes(index)
    ) return;

    if(memoryOpen.length>=2)
        return;

    memoryOpen.push(index);

    renderMemory();

    if(memoryOpen.length<2)
        return;

    memoryMoves++;

    memoryBusy=true;

    const a=memoryOpen[0];
    const b=memoryOpen[1];

    if(
        memoryValues[a]
        ===
        memoryValues[b]
    ){

        memoryMatches++;

        memoryOpen=[];

        memoryBusy=false;

        renderMemory();

        if(memoryMatches===15){

            memoryRunning=false;

            closeModal(
                "memoryModal"
            );

            finishServerGame(
                "memory",
                "win"
            );

        }

    }else{

        setTimeout(
            ()=>{

                memoryOpen=[];

                memoryBusy=false;

                renderMemory();

                if(memoryMoves>=38){

                    memoryRunning=false;

                    closeModal(
                        "memoryModal"
                    );

                    finishServerGame(
                        "memory",
                        "lose"
                    );

                }

            },
            700
        );

    }

}


/* ============================================================
WORD ESCAPE
============================================================ */

const riddles=[
    {"q": "Mere paas keys hoti hain lekin main locks nahi kholta; mere paas space bhi hoti hai lekin room nahi. Main kya hoon?", "a": "keyboard", "h": "Yeh computer ke saath use hoti hai."},
    {"q": "Mere paas keys hoti hain lekin main locks nahi kholta; mere paas space bhi hoti hai lekin room nahi. Guess karo main kya hoon?", "a": "keyboard", "h": "Yeh computer ke saath use hoti hai."},
    {"q": "Zara dhyan se socho: Mere paas keys hoti hain lekin main locks nahi kholta; mere paas space bhi hoti hai lekin room nahi. Main kya hoon?", "a": "keyboard", "h": "Yeh computer ke saath use hoti hai."},
    {"q": "Mere paas keys hoti hain lekin main locks nahi kholta; mere paas space bhi hoti hai lekin room nahi. Mera jawab kya hai?", "a": "keyboard", "h": "Yeh computer ke saath use hoti hai."},
    {"q": "Mere paas bohat se teeth hote hain lekin main kaat nahi sakta. Main kya hoon?", "a": "comb", "h": "Yeh baalon ko set karne ke kaam aata hai."},
    {"q": "Mere paas bohat se teeth hote hain lekin main kaat nahi sakta. Guess karo main kya hoon?", "a": "comb", "h": "Yeh baalon ko set karne ke kaam aata hai."},
    {"q": "Zara dhyan se socho: Mere paas bohat se teeth hote hain lekin main kaat nahi sakta. Main kya hoon?", "a": "comb", "h": "Yeh baalon ko set karne ke kaam aata hai."},
    {"q": "Mere paas bohat se teeth hote hain lekin main kaat nahi sakta. Mera jawab kya hai?", "a": "comb", "h": "Yeh baalon ko set karne ke kaam aata hai."},
    {"q": "Main jitna kisi cheez ko dry karta hoon, utna hi khud wet hota jata hoon. Main kya hoon?", "a": "towel", "h": "Bathroom mein aam milta hoon."},
    {"q": "Main jitna kisi cheez ko dry karta hoon, utna hi khud wet hota jata hoon. Guess karo main kya hoon?", "a": "towel", "h": "Bathroom mein aam milta hoon."},
    {"q": "Zara dhyan se socho: Main jitna kisi cheez ko dry karta hoon, utna hi khud wet hota jata hoon. Main kya hoon?", "a": "towel", "h": "Bathroom mein aam milta hoon."},
    {"q": "Main jitna kisi cheez ko dry karta hoon, utna hi khud wet hota jata hoon. Mera jawab kya hai?", "a": "towel", "h": "Bathroom mein aam milta hoon."},
    {"q": "Meri awaaz tum sun sakte ho, lekin mera koi munh ya jism nahi. Main kya hoon?", "a": "echo", "h": "Paharon ya khaali jagah mein meri awaaz sunai de sakti hai."},
    {"q": "Meri awaaz tum sun sakte ho, lekin mera koi munh ya jism nahi. Guess karo main kya hoon?", "a": "echo", "h": "Paharon ya khaali jagah mein meri awaaz sunai de sakti hai."},
    {"q": "Zara dhyan se socho: Meri awaaz tum sun sakte ho, lekin mera koi munh ya jism nahi. Main kya hoon?", "a": "echo", "h": "Paharon ya khaali jagah mein meri awaaz sunai de sakti hai."},
    {"q": "Meri awaaz tum sun sakte ho, lekin mera koi munh ya jism nahi. Mera jawab kya hai?", "a": "echo", "h": "Paharon ya khaali jagah mein meri awaaz sunai de sakti hai."},
    {"q": "Mere hands hain lekin main clap nahi kar sakta. Main kya hoon?", "a": "clock", "h": "Time batane mein madad karta hoon."},
    {"q": "Mere hands hain lekin main clap nahi kar sakta. Guess karo main kya hoon?", "a": "clock", "h": "Time batane mein madad karta hoon."},
    {"q": "Zara dhyan se socho: Mere hands hain lekin main clap nahi kar sakta. Main kya hoon?", "a": "clock", "h": "Time batane mein madad karta hoon."},
    {"q": "Mere hands hain lekin main clap nahi kar sakta. Mera jawab kya hai?", "a": "clock", "h": "Time batane mein madad karta hoon."},
    {"q": "Main tumhare saath chalti hoon lekin raat ke andhere mein gayab ho jati hoon. Main kya hoon?", "a": "shadow", "h": "Roshni aur tumhare darmiyan kuch socho."},
    {"q": "Main tumhare saath chalti hoon lekin raat ke andhere mein gayab ho jati hoon. Guess karo main kya hoon?", "a": "shadow", "h": "Roshni aur tumhare darmiyan kuch socho."},
    {"q": "Zara dhyan se socho: Main tumhare saath chalti hoon lekin raat ke andhere mein gayab ho jati hoon. Main kya hoon?", "a": "shadow", "h": "Roshni aur tumhare darmiyan kuch socho."},
    {"q": "Main tumhare saath chalti hoon lekin raat ke andhere mein gayab ho jati hoon. Mera jawab kya hai?", "a": "shadow", "h": "Roshni aur tumhare darmiyan kuch socho."},
    {"q": "Main aasman se girta hoon aur zameen ko geela karta hoon. Main kya hoon?", "a": "rain", "h": "Clouds se aata hoon."},
    {"q": "Main aasman se girta hoon aur zameen ko geela karta hoon. Guess karo main kya hoon?", "a": "rain", "h": "Clouds se aata hoon."},
    {"q": "Zara dhyan se socho: Main aasman se girta hoon aur zameen ko geela karta hoon. Main kya hoon?", "a": "rain", "h": "Clouds se aata hoon."},
    {"q": "Main aasman se girta hoon aur zameen ko geela karta hoon. Mera jawab kya hai?", "a": "rain", "h": "Clouds se aata hoon."},
    {"q": "Mujhe khana do to main barhta hoon, lekin paani do to khatam ho jata hoon. Main kya hoon?", "a": "fire", "h": "Heat aur flame se related hoon."},
    {"q": "Mujhe khana do to main barhta hoon, lekin paani do to khatam ho jata hoon. Guess karo main kya hoon?", "a": "fire", "h": "Heat aur flame se related hoon."},
    {"q": "Zara dhyan se socho: Mujhe khana do to main barhta hoon, lekin paani do to khatam ho jata hoon. Main kya hoon?", "a": "fire", "h": "Heat aur flame se related hoon."},
    {"q": "Mujhe khana do to main barhta hoon, lekin paani do to khatam ho jata hoon. Mera jawab kya hai?", "a": "fire", "h": "Heat aur flame se related hoon."},
    {"q": "Main jalta hoon aur dheere dheere chhota hota jata hoon; meri roshni andhere mein kaam aati hai. Main kya hoon?", "a": "candle", "h": "Birthday par bhi mujhe dekha jata hai."},
    {"q": "Main jalta hoon aur dheere dheere chhota hota jata hoon; meri roshni andhere mein kaam aati hai. Guess karo main kya hoon?", "a": "candle", "h": "Birthday par bhi mujhe dekha jata hai."},
    {"q": "Zara dhyan se socho: Main jalta hoon aur dheere dheere chhota hota jata hoon; meri roshni andhere mein kaam aati hai. Main kya hoon?", "a": "candle", "h": "Birthday par bhi mujhe dekha jata hai."},
    {"q": "Main jalta hoon aur dheere dheere chhota hota jata hoon; meri roshni andhere mein kaam aati hai. Mera jawab kya hai?", "a": "candle", "h": "Birthday par bhi mujhe dekha jata hai."},
    {"q": "Mere pages hote hain lekin main tree nahi; mere andar kahaniyan aur maloomat hoti hain. Main kya hoon?", "a": "book", "h": "Mujhe read kiya jata hai."},
    {"q": "Mere pages hote hain lekin main tree nahi; mere andar kahaniyan aur maloomat hoti hain. Guess karo main kya hoon?", "a": "book", "h": "Mujhe read kiya jata hai."},
    {"q": "Zara dhyan se socho: Mere pages hote hain lekin main tree nahi; mere andar kahaniyan aur maloomat hoti hain. Main kya hoon?", "a": "book", "h": "Mujhe read kiya jata hai."},
    {"q": "Mere pages hote hain lekin main tree nahi; mere andar kahaniyan aur maloomat hoti hain. Mera jawab kya hai?", "a": "book", "h": "Mujhe read kiya jata hai."},
    {"q": "Main tumhara chehra dikha sakta hoon lekin khud tum nahi hoon. Main kya hoon?", "a": "mirror", "h": "Glass jaisi surface par reflection dekho."},
    {"q": "Main tumhara chehra dikha sakta hoon lekin khud tum nahi hoon. Guess karo main kya hoon?", "a": "mirror", "h": "Glass jaisi surface par reflection dekho."},
    {"q": "Zara dhyan se socho: Main tumhara chehra dikha sakta hoon lekin khud tum nahi hoon. Main kya hoon?", "a": "mirror", "h": "Glass jaisi surface par reflection dekho."},
    {"q": "Main tumhara chehra dikha sakta hoon lekin khud tum nahi hoon. Mera jawab kya hai?", "a": "mirror", "h": "Glass jaisi surface par reflection dekho."},
    {"q": "Main deewar mein hota hoon aur mere through tum bahar dekh sakte ho, lekin main darwaza nahi. Main kya hoon?", "a": "window", "h": "Mujhe khola bhi ja sakta hai."},
    {"q": "Main deewar mein hota hoon aur mere through tum bahar dekh sakte ho, lekin main darwaza nahi. Guess karo main kya hoon?", "a": "window", "h": "Mujhe khola bhi ja sakta hai."},
    {"q": "Zara dhyan se socho: Main deewar mein hota hoon aur mere through tum bahar dekh sakte ho, lekin main darwaza nahi. Main kya hoon?", "a": "window", "h": "Mujhe khola bhi ja sakta hai."},
    {"q": "Main deewar mein hota hoon aur mere through tum bahar dekh sakte ho, lekin main darwaza nahi. Mera jawab kya hai?", "a": "window", "h": "Mujhe khola bhi ja sakta hai."},
    {"q": "Main ghar ke andar jane ka rasta deta hoon, lekin main road nahi. Main kya hoon?", "a": "door", "h": "Mere paas handle ya knob ho sakta hai."},
    {"q": "Main ghar ke andar jane ka rasta deta hoon, lekin main road nahi. Guess karo main kya hoon?", "a": "door", "h": "Mere paas handle ya knob ho sakta hai."},
    {"q": "Zara dhyan se socho: Main ghar ke andar jane ka rasta deta hoon, lekin main road nahi. Main kya hoon?", "a": "door", "h": "Mere paas handle ya knob ho sakta hai."},
    {"q": "Main ghar ke andar jane ka rasta deta hoon, lekin main road nahi. Mera jawab kya hai?", "a": "door", "h": "Mere paas handle ya knob ho sakta hai."},
    {"q": "Main behte hue safar karta hoon, mera pani samandar ki taraf ja sakta hai, lekin main road nahi. Main kya hoon?", "a": "river", "h": "Pani ka natural rasta socho."},
    {"q": "Main behte hue safar karta hoon, mera pani samandar ki taraf ja sakta hai, lekin main road nahi. Guess karo main kya hoon?", "a": "river", "h": "Pani ka natural rasta socho."},
    {"q": "Zara dhyan se socho: Main behte hue safar karta hoon, mera pani samandar ki taraf ja sakta hai, lekin main road nahi. Main kya hoon?", "a": "river", "h": "Pani ka natural rasta socho."},
    {"q": "Main behte hue safar karta hoon, mera pani samandar ki taraf ja sakta hai, lekin main road nahi. Mera jawab kya hai?", "a": "river", "h": "Pani ka natural rasta socho."},
    {"q": "Main bohat uncha ho sakta hoon aur mere paas choti hoti hai, lekin main building nahi. Main kya hoon?", "a": "mountain", "h": "Nature ka bohat bara landform hoon."},
    {"q": "Main bohat uncha ho sakta hoon aur mere paas choti hoti hai, lekin main building nahi. Guess karo main kya hoon?", "a": "mountain", "h": "Nature ka bohat bara landform hoon."},
    {"q": "Zara dhyan se socho: Main bohat uncha ho sakta hoon aur mere paas choti hoti hai, lekin main building nahi. Main kya hoon?", "a": "mountain", "h": "Nature ka bohat bara landform hoon."},
    {"q": "Main bohat uncha ho sakta hoon aur mere paas choti hoti hai, lekin main building nahi. Mera jawab kya hai?", "a": "mountain", "h": "Nature ka bohat bara landform hoon."},
    {"q": "Main raat ke aasman mein nazar aata hoon aur meri shape badalti hui lag sakti hai. Main kya hoon?", "a": "moon", "h": "Night sky ki ek mashhoor cheez."},
    {"q": "Main raat ke aasman mein nazar aata hoon aur meri shape badalti hui lag sakti hai. Guess karo main kya hoon?", "a": "moon", "h": "Night sky ki ek mashhoor cheez."},
    {"q": "Zara dhyan se socho: Main raat ke aasman mein nazar aata hoon aur meri shape badalti hui lag sakti hai. Main kya hoon?", "a": "moon", "h": "Night sky ki ek mashhoor cheez."},
    {"q": "Main raat ke aasman mein nazar aata hoon aur meri shape badalti hui lag sakti hai. Mera jawab kya hai?", "a": "moon", "h": "Night sky ki ek mashhoor cheez."},
    {"q": "Main subah aasman mein aata hoon, roshni aur heat deta hoon, lekin main bulb nahi. Main kya hoon?", "a": "sun", "h": "Daytime ka sab se roshan celestial object socho."},
    {"q": "Main subah aasman mein aata hoon, roshni aur heat deta hoon, lekin main bulb nahi. Guess karo main kya hoon?", "a": "sun", "h": "Daytime ka sab se roshan celestial object socho."},
    {"q": "Zara dhyan se socho: Main subah aasman mein aata hoon, roshni aur heat deta hoon, lekin main bulb nahi. Main kya hoon?", "a": "sun", "h": "Daytime ka sab se roshan celestial object socho."},
    {"q": "Main subah aasman mein aata hoon, roshni aur heat deta hoon, lekin main bulb nahi. Mera jawab kya hai?", "a": "sun", "h": "Daytime ka sab se roshan celestial object socho."},
    {"q": "Main aasman mein tairta hoon aur kabhi kabhi mere andar se rain aati hai. Main kya hoon?", "a": "cloud", "h": "White ya grey sky object."},
    {"q": "Main aasman mein tairta hoon aur kabhi kabhi mere andar se rain aati hai. Guess karo main kya hoon?", "a": "cloud", "h": "White ya grey sky object."},
    {"q": "Zara dhyan se socho: Main aasman mein tairta hoon aur kabhi kabhi mere andar se rain aati hai. Main kya hoon?", "a": "cloud", "h": "White ya grey sky object."},
    {"q": "Main aasman mein tairta hoon aur kabhi kabhi mere andar se rain aati hai. Mera jawab kya hai?", "a": "cloud", "h": "White ya grey sky object."},
    {"q": "Tum mujhe dekh nahi sakte, lekin meri movement ko feel kar sakte ho. Main kya hoon?", "a": "wind", "h": "Patton ko hilane wali invisible cheez."},
    {"q": "Tum mujhe dekh nahi sakte, lekin meri movement ko feel kar sakte ho. Guess karo main kya hoon?", "a": "wind", "h": "Patton ko hilane wali invisible cheez."},
    {"q": "Zara dhyan se socho: Tum mujhe dekh nahi sakte, lekin meri movement ko feel kar sakte ho. Main kya hoon?", "a": "wind", "h": "Patton ko hilane wali invisible cheez."},
    {"q": "Tum mujhe dekh nahi sakte, lekin meri movement ko feel kar sakte ho. Mera jawab kya hai?", "a": "wind", "h": "Patton ko hilane wali invisible cheez."},
    {"q": "Main pani ka thanda solid form hoon aur heat milne par pighal jata hoon. Main kya hoon?", "a": "ice", "h": "Freezer mein milta hoon."},
    {"q": "Main pani ka thanda solid form hoon aur heat milne par pighal jata hoon. Guess karo main kya hoon?", "a": "ice", "h": "Freezer mein milta hoon."},
    {"q": "Zara dhyan se socho: Main pani ka thanda solid form hoon aur heat milne par pighal jata hoon. Main kya hoon?", "a": "ice", "h": "Freezer mein milta hoon."},
    {"q": "Main pani ka thanda solid form hoon aur heat milne par pighal jata hoon. Mera jawab kya hai?", "a": "ice", "h": "Freezer mein milta hoon."},
    {"q": "Main tumhe door baithe insan se baat karwa sakta hoon aur pocket mein aa sakta hoon. Main kya hoon?", "a": "phone", "h": "Calls aur messages ke liye use hota hoon."},
    {"q": "Main tumhe door baithe insan se baat karwa sakta hoon aur pocket mein aa sakta hoon. Guess karo main kya hoon?", "a": "phone", "h": "Calls aur messages ke liye use hota hoon."},
    {"q": "Zara dhyan se socho: Main tumhe door baithe insan se baat karwa sakta hoon aur pocket mein aa sakta hoon. Main kya hoon?", "a": "phone", "h": "Calls aur messages ke liye use hota hoon."},
    {"q": "Main tumhe door baithe insan se baat karwa sakta hoon aur pocket mein aa sakta hoon. Mera jawab kya hai?", "a": "phone", "h": "Calls aur messages ke liye use hota hoon."},
    {"q": "Main moments ko capture karta hoon lekin khud photographer nahi. Main kya hoon?", "a": "camera", "h": "Photos banane ke kaam aata hoon."},
    {"q": "Main moments ko capture karta hoon lekin khud photographer nahi. Guess karo main kya hoon?", "a": "camera", "h": "Photos banane ke kaam aata hoon."},
    {"q": "Zara dhyan se socho: Main moments ko capture karta hoon lekin khud photographer nahi. Main kya hoon?", "a": "camera", "h": "Photos banane ke kaam aata hoon."},
    {"q": "Main moments ko capture karta hoon lekin khud photographer nahi. Mera jawab kya hai?", "a": "camera", "h": "Photos banane ke kaam aata hoon."},
    {"q": "Mere paas bohat si keys hoti hain aur main music bana sakta hoon, lekin main computer keyboard nahi. Main kya hoon?", "a": "piano", "h": "Ek musical instrument hoon."},
    {"q": "Mere paas bohat si keys hoti hain aur main music bana sakta hoon, lekin main computer keyboard nahi. Guess karo main kya hoon?", "a": "piano", "h": "Ek musical instrument hoon."},
    {"q": "Zara dhyan se socho: Mere paas bohat si keys hoti hain aur main music bana sakta hoon, lekin main computer keyboard nahi. Main kya hoon?", "a": "piano", "h": "Ek musical instrument hoon."},
    {"q": "Mere paas bohat si keys hoti hain aur main music bana sakta hoon, lekin main computer keyboard nahi. Mera jawab kya hai?", "a": "piano", "h": "Ek musical instrument hoon."},
    {"q": "Mere paas strings hoti hain aur mujhe strum karke music nikala jata hai. Main kya hoon?", "a": "guitar", "h": "Music instrument, strings wala."},
    {"q": "Mere paas strings hoti hain aur mujhe strum karke music nikala jata hai. Guess karo main kya hoon?", "a": "guitar", "h": "Music instrument, strings wala."},
    {"q": "Zara dhyan se socho: Mere paas strings hoti hain aur mujhe strum karke music nikala jata hai. Main kya hoon?", "a": "guitar", "h": "Music instrument, strings wala."},
    {"q": "Mere paas strings hoti hain aur mujhe strum karke music nikala jata hai. Mera jawab kya hai?", "a": "guitar", "h": "Music instrument, strings wala."},
    {"q": "Main barish mein tumhe dry rakhne ke liye sar ke upar khulta hoon. Main kya hoon?", "a": "umbrella", "h": "Rainy weather mein saath rakha jata hai."},
    {"q": "Main barish mein tumhe dry rakhne ke liye sar ke upar khulta hoon. Guess karo main kya hoon?", "a": "umbrella", "h": "Rainy weather mein saath rakha jata hai."},
    {"q": "Zara dhyan se socho: Main barish mein tumhe dry rakhne ke liye sar ke upar khulta hoon. Main kya hoon?", "a": "umbrella", "h": "Rainy weather mein saath rakha jata hai."},
    {"q": "Main barish mein tumhe dry rakhne ke liye sar ke upar khulta hoon. Mera jawab kya hai?", "a": "umbrella", "h": "Rainy weather mein saath rakha jata hai."},
    {"q": "Main chhoti si cheez hoon jo aksar lock kholne mein kaam aati hoon. Main kya hoon?", "a": "key", "h": "Lock ke saath mera connection hai."},
    {"q": "Main chhoti si cheez hoon jo aksar lock kholne mein kaam aati hoon. Guess karo main kya hoon?", "a": "key", "h": "Lock ke saath mera connection hai."},
    {"q": "Zara dhyan se socho: Main chhoti si cheez hoon jo aksar lock kholne mein kaam aati hoon. Main kya hoon?", "a": "key", "h": "Lock ke saath mera connection hai."},
    {"q": "Main chhoti si cheez hoon jo aksar lock kholne mein kaam aati hoon. Mera jawab kya hai?", "a": "key", "h": "Lock ke saath mera connection hai."},
    {"q": "Main tumhare cards aur cash ko ek jagah rakhta hoon, lekin main pocket nahi. Main kya hoon?", "a": "wallet", "h": "Paise carry karne ke liye use hota hoon."},
    {"q": "Main tumhare cards aur cash ko ek jagah rakhta hoon, lekin main pocket nahi. Guess karo main kya hoon?", "a": "wallet", "h": "Paise carry karne ke liye use hota hoon."},
    {"q": "Zara dhyan se socho: Main tumhare cards aur cash ko ek jagah rakhta hoon, lekin main pocket nahi. Main kya hoon?", "a": "wallet", "h": "Paise carry karne ke liye use hota hoon."},
    {"q": "Main tumhare cards aur cash ko ek jagah rakhta hoon, lekin main pocket nahi. Mera jawab kya hai?", "a": "wallet", "h": "Paise carry karne ke liye use hota hoon."},
    {"q": "Mere pages ya boxes mein dates hoti hain aur main batata hoon aaj ka din kya hai. Main kya hoon?", "a": "calendar", "h": "Months aur dates se related hoon."},
    {"q": "Mere pages ya boxes mein dates hoti hain aur main batata hoon aaj ka din kya hai. Guess karo main kya hoon?", "a": "calendar", "h": "Months aur dates se related hoon."},
    {"q": "Zara dhyan se socho: Mere pages ya boxes mein dates hoti hain aur main batata hoon aaj ka din kya hai. Main kya hoon?", "a": "calendar", "h": "Months aur dates se related hoon."},
    {"q": "Mere pages ya boxes mein dates hoti hain aur main batata hoon aaj ka din kya hai. Mera jawab kya hai?", "a": "calendar", "h": "Months aur dates se related hoon."},
    {"q": "Main tumhari kalai par reh kar time batata hoon. Main kya hoon?", "a": "watch", "h": "Clock ka chhota wearable version socho."},
    {"q": "Main tumhari kalai par reh kar time batata hoon. Guess karo main kya hoon?", "a": "watch", "h": "Clock ka chhota wearable version socho."},
    {"q": "Zara dhyan se socho: Main tumhari kalai par reh kar time batata hoon. Main kya hoon?", "a": "watch", "h": "Clock ka chhota wearable version socho."},
    {"q": "Main tumhari kalai par reh kar time batata hoon. Mera jawab kya hai?", "a": "watch", "h": "Clock ka chhota wearable version socho."},
    {"q": "Main tumhe jagahen dhoondhne aur raasta samajhne mein madad karta hoon, lekin main road nahi. Main kya hoon?", "a": "map", "h": "Countries, cities ya roads meri madad se dekho."},
    {"q": "Main tumhe jagahen dhoondhne aur raasta samajhne mein madad karta hoon, lekin main road nahi. Guess karo main kya hoon?", "a": "map", "h": "Countries, cities ya roads meri madad se dekho."},
    {"q": "Zara dhyan se socho: Main tumhe jagahen dhoondhne aur raasta samajhne mein madad karta hoon, lekin main road nahi. Main kya hoon?", "a": "map", "h": "Countries, cities ya roads meri madad se dekho."},
    {"q": "Main tumhe jagahen dhoondhne aur raasta samajhne mein madad karta hoon, lekin main road nahi. Mera jawab kya hai?", "a": "map", "h": "Countries, cities ya roads meri madad se dekho."}
];

let weIndex=0;
let weTimer=20;
let weInterval=null;
let weRunning=false;
let weRound=[];
let weRecent=[];

function chooseWordEscapeRiddles(){
    const recentSet=new Set(weRecent);
    const available=riddles.map((_,i)=>i).filter(i=>!recentSet.has(i));
    for(let i=available.length-1;i>0;i--){
        const j=Math.floor(Math.random()*(i+1));
        [available[i],available[j]]=[available[j],available[i]];
    }
    weRound=available.slice(0,5);
    weRecent=weRecent.concat(weRound).slice(-100);
    try{ localStorage.setItem("sa_we_recent",JSON.stringify(weRecent)); }catch(e){}
}
async function startWordEscape(){

    if(
        !(await startServerGame(
            "word_escape"
        ))
    ) return;

    weRunning=true;
    weIndex=0;
    weTimer=20;
    clearInterval(weInterval);
    weRecent=JSON.parse(localStorage.getItem("sa_we_recent") || "[]");
    chooseWordEscapeRiddles();
    document.getElementById("weLevel").innerText="1";
    document.getElementById("weTimer").innerText="20 seconds";
    document.getElementById("weMessage").innerText="";
    document.getElementById("weAnswer").value="";
    document.getElementById("weRiddle").innerText=riddles[weRound[0]].q;

    openModal(
        "wordEscapeModal"
    );

    weInterval=setInterval(()=>{
        if(!weRunning) return;
        weTimer--;
        document.getElementById("weTimer").innerText=weTimer+" seconds";
        if(weTimer<=0){
            clearInterval(weInterval);
            weRunning=false;
            closeModal("wordEscapeModal");
            finishServerGame("word_escape","lose");
        }
    },1000);
}

function showRiddleHint(){
    if(!weRunning || !weRound.length) return;
    document.getElementById("weMessage").innerText=
        "💡 Hint: "+riddles[weRound[weIndex]].h;
}

function checkRiddle(){
    if(!weRunning || !weRound.length) return;

    const answer=document.getElementById("weAnswer").value.trim().toLowerCase();
    const current=riddles[weRound[weIndex]];

    if(answer!==current.a){
        document.getElementById("weMessage").innerText="❌ Wrong answer. Try again!";
        return;
    }

    weIndex++;

    if(weIndex>=weRound.length){
        clearInterval(weInterval);
        weRunning=false;
        closeModal("wordEscapeModal");
        finishServerGame("word_escape","win");
        return;
    }

    weTimer=20;
    document.getElementById("weLevel").innerText=String(weIndex+1);
    document.getElementById("weTimer").innerText="20 seconds";
    document.getElementById("weRiddle").innerText=riddles[weRound[weIndex]].q;
    document.getElementById("weAnswer").value="";
    document.getElementById("weMessage").innerText="✅ Correct! Next riddle.";
}

/* ============================================================
WORD SEARCH
============================================================ */

const searchWords=[
    "SKILL","ARENA","FOMO","GAMES","WIN",
    "PUZZLE","MEMORY","RIDDLE","SCORE","PLAYER"
];

let wsGrid=[];
let wsFound=[];
let wsSelecting="";
let wsSelectingCells=[];
let wsRunning=false;
let wsTimer=75;
let wsInterval=null;


async function startWordSearch(){

    if(
        !(await startServerGame(
            "word_search"
        ))
    ) return;

    wsRunning=true;

    wsFound=[];
    wsSelecting="";
    wsSelectingCells=[];
    clearInterval(wsInterval);
    wsTimer=75;
    document.getElementById("wsTimer").innerText="75s";

    createWordSearch();

    openModal(
        "wordSearchModal"
    );

    wsInterval=setInterval(()=>{
        if(!wsRunning) return;
        wsTimer--;
        document.getElementById("wsTimer").innerText=wsTimer+"s";
        if(wsTimer<=0){
            clearInterval(wsInterval);
            wsRunning=false;
            closeModal("wordSearchModal");
            finishServerGame("word_search","lose");
        }
    },1000);

}


function createWordSearch(){

    const size=8;

    wsGrid =
        Array.from(
            {length:size},
            ()=>Array(size)
                .fill("")
        );

    // Place words horizontally/vertically
    searchWords.forEach(
        word=>{

            let placed=false;

            for(
                let attempts=0;
                attempts<100
                &&
                !placed;
                attempts++
            ){

                const horizontal =
                    Math.random()>.5;

                const row =
                    Math.floor(
                        Math.random()*size
                    );

                const col =
                    Math.floor(
                        Math.random()*size
                    );

                if(horizontal){

                    if(
                        col+word.length
                        >size
                    ) continue;

                    let ok=true;

                    for(
                        let i=0;
                        i<word.length;
                        i++
                    ){

                        if(
                            wsGrid[row][col+i]
                            &&
                            wsGrid[row][col+i]
                            !==word[i]
                        ){

                            ok=false;
                            break;

                        }

                    }

                    if(!ok) continue;

                    for(
                        let i=0;
                        i<word.length;
                        i++
                    ){

                        wsGrid[row][col+i]
                            =word[i];

                    }

                    placed=true;

                }else{

                    if(
                        row+word.length
                        >size
                    ) continue;

                    let ok=true;

                    for(
                        let i=0;
                        i<word.length;
                        i++
                    ){

                        if(
                            wsGrid[row+i][col]
                            &&
                            wsGrid[row+i][col]
                            !==word[i]
                        ){

                            ok=false;
                            break;

                        }

                    }

                    if(!ok) continue;

                    for(
                        let i=0;
                        i<word.length;
                        i++
                    ){

                        wsGrid[row+i][col]
                            =word[i];

                    }

                    placed=true;

                }

            }

        }
    );

    const letters=
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

    for(
        let r=0;
        r<size;
        r++
    ){

        for(
            let c=0;
            c<size;
            c++
        ){

            if(!wsGrid[r][c]){

                wsGrid[r][c]
                    =
                    letters[
                        Math.floor(
                            Math.random()
                            *letters.length
                        )
                    ];

            }

        }

    }

    renderWordSearch();

}


function renderWordSearch(){

    document.getElementById(
        "wsWords"
    ).innerHTML =
        searchWords
        .map(
            w=>
            `<span
             style="margin:5px">
             ${w}
             </span>`
        )
        .join("");

    const grid =
        document.getElementById(
            "wsGrid"
        );

    grid.innerHTML="";

    wsGrid.forEach(
        (row,r)=>{

            row.forEach(
                (letter,c)=>{

                    const b =
                        document.createElement(
                            "button"
                        );

                    b.className=
                        "word-cell";

                    b.innerText=
                        letter;

                    if(wsSelectingCells.some(
                        cell=>cell[0]===r && cell[1]===c
                    )){
                        b.classList.add("selected");
                    }

                    b.onpointerdown=(e)=>{
                        e.preventDefault();
                        selectWord(
                            r,
                            c
                        );
                    };

                    grid.appendChild(
                        b
                    );

                }
            );

        }
    );

    document.getElementById(
        "wsFound"
    ).innerText=
        wsFound.length;

}


function selectWord(r,c){

    if(!wsRunning)
        return;

    // Prevent selecting the same cell twice in one attempt.
    if(wsSelectingCells.some(
        cell=>cell[0]===r && cell[1]===c
    )) return;

    wsSelecting += wsGrid[r][c];
    wsSelectingCells.push([r,c]);

    // Check the selected sequence BEFORE redrawing the grid.
    // This keeps the visible selection and found counter in sync.
    if(searchWords.includes(wsSelecting)){

        if(!wsFound.includes(wsSelecting)){
            wsFound.push(wsSelecting);
        }

        wsSelecting="";
        wsSelectingCells=[];
        renderWordSearch();

        if(wsFound.length===searchWords.length){
            wsRunning=false;
            clearInterval(wsInterval);
            closeModal("wordSearchModal");
            finishServerGame("word_search","win");
        }
        return;
    }

    // Keep the current selection only while it is a valid prefix.
    if(!searchWords.some(
        w=>w.startsWith(wsSelecting)
    )){
        wsSelecting="";
        wsSelectingCells=[];
    }

    renderWordSearch();
}


/* ============================================================
CAR ESCAPE — 4 LANE MODE
============================================================ */

let carRunning=false;
let carScore=0;
let carLane=1;
let traffic=[];
let carFrame=0;
let carSpawn=0;
let carSpeed=2.8;
let carTokenReady=false;

const carCanvas =
    document.getElementById(
        "carCanvas"
    );

const carCtx =
    carCanvas.getContext(
        "2d"
    );


async function startCarEscape(){

    if(
        !(await startServerGame(
            "car_escape"
        ))
    ) return;

    carRunning=true;
    carScore=0;
    carLane=1;
    traffic=[];
    carFrame=0;
    carSpawn=0;
    carSpeed=2.8;

    document.getElementById(
        "carScore"
    ).innerText="0";

    openModal(
        "carModal"
    );

    requestAnimationFrame(
        carLoop
    );

}


function moveCar(direction){

    if(!carRunning)
        return;

    carLane += direction;

    if(carLane<0)
        carLane=0;

    if(carLane>3)
        carLane=3;

}


function carPlayer(){

    return {
        x:62 + carLane*90,
        y:545,
        w:50,
        h:82
    };

}


function drawCarRoad(){

    carCtx.clearRect(
        0,
        0,
        420,
        650
    );

    // Grass
    carCtx.fillStyle=
        "#064e3b";

    carCtx.fillRect(
        0,
        0,
        420,
        650
    );

    // Road
    carCtx.fillStyle=
        "#1f2937";

    carCtx.fillRect(
        25,
        0,
        370,
        650
    );

    // Road borders
    carCtx.fillStyle=
        "#e5e7eb";

    carCtx.fillRect(
        25,
        0,
        5,
        650
    );

    carCtx.fillRect(
        395,
        0,
        5,
        650
    );

    // Moving lane markings
    carCtx.strokeStyle=
        "#dbeafe";

    carCtx.lineWidth=4;

    carCtx.setLineDash(
        [35,30]
    );

    carCtx.lineDashOffset =
        -(carFrame*carSpeed)%65;

    [115,205,295].forEach(function(x){
        carCtx.beginPath();
        carCtx.moveTo(x,0);
        carCtx.lineTo(x,650);
        carCtx.stroke();
    });

    carCtx.setLineDash([]);

}


function drawPlayer(){

    const p=
        carPlayer();

    carCtx.fillStyle=
        "#22c55e";

    carCtx.beginPath();

    carCtx.roundRect(
        p.x,
        p.y,
        p.w,
        p.h,
        9
    );

    carCtx.fill();

    carCtx.fillStyle=
        "#0f172a";

    carCtx.fillRect(
        p.x+8,
        p.y+12,
        34,
        25
    );

    carCtx.fillRect(
        p.x+8,
        p.y+47,
        34,
        20
    );

    carCtx.fillStyle=
        "#fef08a";

    carCtx.fillRect(
        p.x+5,
        p.y+5,
        10,
        7
    );

    carCtx.fillRect(
        p.x+35,
        p.y+5,
        10,
        7
    );

}


function spawnTraffic(){

    const lane =
        Math.floor(
            Math.random()*4
        );

    traffic.push({
        lane,
        x:62+lane*90,
        y:-100,
        w:50,
        h:82,
        speed:
            carSpeed
            +Math.random()*1.2,
        color:[
            "#ef4444",
            "#3b82f6",
            "#f97316",
            "#a855f7",
            "#eab308"
        ][
            Math.floor(
                Math.random()*5
            )
        ]
    });


}


function collision(a,b){

    const pad=8;

    return (
        a.x+pad
        <
        b.x+b.w-pad
        &&
        a.x+a.w-pad
        >
        b.x+pad
        &&
        a.y+pad
        <
        b.y+b.h-pad
        &&
        a.y+a.h-pad
        >
        b.y+pad
    );

}


function carLoop(){

    if(!carRunning)
        return;

    carFrame++;

    drawCarRoad();

    // Increase difficulty
    carSpeed =
        2.8
        +carScore/500;

    // Faster spawning
    const interval =
        Math.max(
            48,
            72
            -Math.floor(
                carScore/80
            )
        );

    carSpawn++;

    if(
        carSpawn>=interval
    ){

        spawnTraffic();

        carSpawn=0;

    }

    const player =
        carPlayer();

    drawPlayer();

    for(
        let i=traffic.length-1;
        i>=0;
        i--
    ){

        const t=
            traffic[i];

        t.y += t.speed;

        carCtx.fillStyle=
            t.color;

        carCtx.beginPath();

        carCtx.roundRect(
            t.x,
            t.y,
            t.w,
            t.h,
            9
        );

        carCtx.fill();

        carCtx.fillStyle=
            "#111827";

        carCtx.fillRect(
            t.x+8,
            t.y+12,
            34,
            24
        );

        carCtx.fillRect(
            t.x+8,
            t.y+48,
            34,
            20
        );

        carCtx.fillStyle=
            "#ef4444";

        carCtx.fillRect(
            t.x+5,
            t.y+5,
            10,
            7
        );

        carCtx.fillRect(
            t.x+35,
            t.y+5,
            10,
            7
        );


        if(
            collision(
                player,
                t
            )
        ){

            carRunning=false;

            closeModal(
                "carModal"
            );

            finishServerGame(
                "car_escape",
                "lose"
            );

            return;

        }


        if(
            t.y>650
        ){

            traffic.splice(
                i,
                1
            );

            carScore += 8;

            document.getElementById(
                "carScore"
            ).innerText=
                carScore;

            if(
                carScore>=1000
            ){

                carRunning=false;

                closeModal(
                    "carModal"
                );

                finishServerGame(
                    "car_escape",
                    "win"
                );

                return;

            }

        }

    }

    requestAnimationFrame(
        carLoop
    );

}


/* Keyboard controls */

document.addEventListener(
    "keydown",
    e=>{

        if(!carRunning)
            return;

        if(
            e.key==="ArrowLeft"
        ){

            moveCar(-1);

            e.preventDefault();

        }

        if(
            e.key==="ArrowRight"
        ){

            moveCar(1);

            e.preventDefault();

        }

    }
);


/* Swipe controls */

let touchStartX=0;

carCanvas.addEventListener(
    "touchstart",
    e=>{

        touchStartX =
            e.touches[0].clientX;

    },
    {
        passive:true
    }
);


carCanvas.addEventListener(
    "touchend",
    e=>{

        const endX =
            e.changedTouches[0].clientX;

        const diff =
            endX-touchStartX;

        if(
            Math.abs(diff)>30
        ){

            moveCar(
                diff<0
                ? -1
                : 1
            );

        }

    },
    {
        passive:true
    }
);


function quitCar(){

    if(!carRunning){

        closeModal(
            "carModal"
        );

        return;

    }

    if(
        !confirm(
            "Quit karne par -5 FOMO Coins lagenge. Continue?"
        )
    ) return;

    carRunning=false;

    closeModal(
        "carModal"
    );

    finishServerGame(
        "car_escape",
        "lose"
    );

}


/* ============================================================
GENERIC GAME QUIT
============================================================ */

function quitGame(game){

    if(
        !confirm(
            "Game quit karne par -5 FOMO Coins lagenge. Continue?"
        )
    ) return;

    if(game==="word_escape"){

        weRunning=false;

        clearInterval(
            weInterval
        );

        closeModal(
            "wordEscapeModal"
        );

    }

    if(game==="puzzle"){

        puzzleRunning=false;

        closeModal(
            "puzzleModal"
        );

    }

    if(game==="memory"){

        memoryRunning=false;

        closeModal(
            "memoryModal"
        );

    }

    if(game==="word_search"){

        wsRunning=false;

        closeModal(
            "wordSearchModal"
        );

    }

    finishServerGame(
        game,
        "lose"
    );

}


/* ============================================================
AUTO START
============================================================ */

(async function(){

    await checkSession();

    await refreshUser();

    // Referral from URL
    const params =
        new URLSearchParams(
            location.search
        );

    const ref =
        params.get(
            "ref"
        );

    if(
        ref
        &&
        userId
        &&
        ref!==userId
    ){

        await api(
            "/api/apply-referral",
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify({
                    user_id:userId,
                    device_token:deviceToken,
                    referrer_id:ref
                })
            }
        );

    }

})();

</script>

</body>
</html>
"""


# ============================================================
# ADMIN HTML
# ============================================================

ADMIN_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta
name="viewport"
content="width=device-width,initial-scale=1">

<title>Skill Arena Admin</title>

<style>

body{
    margin:0;
    background:#07111f;
    color:#f8fafc;
    font-family:
        Segoe UI,
        Arial,
        sans-serif;
}

*{
    box-sizing:border-box;
}

.container{
    width:min(1450px,96%);
    margin:25px auto;
}

.header{
    background:#111c2e;
    border:1px solid #2d405b;
    border-radius:15px;
    padding:20px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:15px;
    flex-wrap:wrap;
}

h1{
    margin:0;
    color:#60a5fa;
}

.stats{
    display:grid;
    grid-template-columns:
        repeat(auto-fit,minmax(190px,1fr));
    gap:15px;
    margin:20px 0;
}

.stat{
    background:#111c2e;
    border:1px solid #2d405b;
    border-radius:12px;
    padding:20px;
}

.stat-number{
    font-size:30px;
    font-weight:900;
    color:#34d399;
}

.panel{
    background:#111c2e;
    border:1px solid #2d405b;
    border-radius:15px;
    padding:20px;
    margin:20px 0;
    overflow:auto;
}

h2{
    color:#93c5fd;
}

table{
    width:100%;
    border-collapse:collapse;
    min-width:850px;
}

th{
    background:#1e2c43;
    padding:12px;
    text-align:left;
}

td{
    padding:11px;
    border-bottom:1px solid #263a54;
}

.pending{
    color:#fbbf24;
    font-weight:900;
}

.approved{
    color:#34d399;
    font-weight:900;
}

.rejected{
    color:#f87171;
    font-weight:900;
}

button{
    border:0;
    border-radius:7px;
    padding:8px 11px;
    color:white;
    font-weight:800;
    cursor:pointer;
    margin:2px;
}

.approve{
    background:#059669;
}

.reject{
    background:#dc2626;
}

.refresh{
    background:#2563eb;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div>

<h1>
⚡ Skill Arena Admin
</h1>

<p style="color:#94a3b8">
Payment • Users • Withdrawals • Audit
</p>

</div>

<button
class="refresh"
onclick="location.reload()">
🔄 Refresh
</button>

</div>


<div class="stats">

<div class="stat">

<div>
👥 Total Users
</div>

<div class="stat-number">
{{ total_users }}
</div>

</div>


<div class="stat">

<div>
⏳ Pending Payments
</div>

<div
class="stat-number"
style="color:#fbbf24">
{{ pending_payments }}
</div>

</div>


<div class="stat">

<div>
💸 Pending Withdrawals
</div>

<div
class="stat-number"
style="color:#fbbf24">
{{ pending_withdrawals }}
</div>

</div>


<div class="stat">

<div>
💎 Total FOMO
</div>

<div
class="stat-number"
style="color:#a78bfa">
{{ total_coins }}
</div>

</div>

</div>


<!-- PAYMENTS -->

<div class="panel">

<h2>
💳 Payment Requests
</h2>

<table>

<tr>

<th>TRX ID</th>
<th>User</th>
<th>Amount</th>
<th>Hours</th>
<th>Status</th>
<th>Action</th>

</tr>

{% for x in transactions %}

<tr>

<td>
<b>{{ x["trx_id"] }}</b>
</td>

<td>
{{ x["user_id"] }}
</td>

<td>
PKR {{ x["amount"] }}
</td>

<td>
{{ x["hours"] }}
</td>

<td
class="{{ x['status'] }}">
{{ x["status"]|upper }}
</td>

<td>

{% if x["status"]=="pending" %}

<button
class="approve"
onclick="paymentAction(
'approve',
'{{ x["trx_id"] }}'
)">
✅ Approve
</button>

<button
class="reject"
onclick="paymentAction(
'reject',
'{{ x["trx_id"] }}'
)">
❌ Reject
</button>

{% else %}

—

{% endif %}

</td>

</tr>

{% endfor %}

</table>

</div>


<!-- WITHDRAWALS -->

<div class="panel">

<h2>
💸 Withdrawal Requests
</h2>

<table>

<tr>

<th>ID</th>
<th>User</th>
<th>Method</th>
<th>Title</th>
<th>Account</th>
<th>FOMO</th>
<th>PKR</th>
<th>Status</th>
<th>Action</th>

</tr>

{% for x in withdrawals %}

<tr>

<td>
{{ x["id"] }}
</td>

<td>
{{ x["user_id"] }}
</td>

<td>
{{ x["method"] }}
</td>

<td>
{{ x["account_title"] }}
</td>

<td>
{{ x["account_number"] }}
</td>

<td>
{{ x["fomo_spent"] }}
</td>

<td>
{{ x["amount_pkr"] }}
</td>

<td
class="{{ x['status'] }}">
{{ x["status"]|upper }}
</td>

<td>

{% if x["status"]=="pending" %}

<button
class="approve"
onclick="withdrawAction(
'approve',
{{ x['id'] }}
)">
💰 Pay & Approve
</button>

<button
class="reject"
onclick="withdrawAction(
'reject',
{{ x['id'] }}
)">
↩️ Reject + Refund
</button>

{% else %}

—

{% endif %}

</td>

</tr>

{% endfor %}

</table>

</div>


<!-- USERS -->

<div class="panel">

<h2>
👥 Users
</h2>

<table>

<tr>

<th>User ID</th>
<th>Email/Mobile</th>
<th>FOMO</th>
<th>Subscription</th>
<th>Created</th>

</tr>

{% for x in users %}

<tr>

<td>
<b>{{ x["user_id"] }}</b>
</td>

<td>
{{ x["identifier"] }}
</td>

<td>
{{ x["fomo_coins"] }}
</td>

<td>
{{ x["subscription_expires_at"] or "No Pass" }}
</td>

<td>
{{ x["created_at"] }}
</td>

</tr>

{% endfor %}

</table>

</div>


<!-- AUDIT -->

<div class="panel">

<h2>
🧾 Admin Audit Log
</h2>

<table>

<tr>

<th>Time</th>
<th>Action</th>
<th>Target</th>
<th>Details</th>

</tr>

{% for x in logs %}

<tr>

<td>
{{ x["created_at"] }}
</td>

<td>
{{ x["action"] }}
</td>

<td>
{{ x["target"] }}
</td>

<td>
{{ x["details"] }}
</td>

</tr>

{% endfor %}

</table>

</div>


</div>


<script>

async function post(
    url,
    body
){

    const r =
        await fetch(
            url,
            {
                method:"POST",
                headers:{
                    "Content-Type":
                    "application/json"
                },
                body:JSON.stringify(body)
            }
        );

    return r.json();

}


async function paymentAction(
    action,
    trx
){

    if(
        !confirm(
            action==="approve"
            ? "Payment approve karein?"
            : "Payment reject karein?"
        )
    ) return;

    const data =
        await post(
            "/api/admin/"
            + action,
            {
                trx_id:trx
            }
        );

    alert(
        data.message
    );

    if(data.success)
        location.reload();

}


async function withdrawAction(
    action,
    id
){

    if(
        !confirm(
            action==="approve"
            ? "Payment kar ke approve karein?"
            : "Reject karke FOMO refund karein?"
        )
    ) return;

    const endpoint =
        action==="approve"
        ? "/api/admin/approve-withdrawal"
        : "/api/admin/reject-withdrawal";

    const data =
        await post(
            endpoint,
            {
                id:id
            }
        );

    alert(
        data.message
    );

    if(data.success)
        location.reload();

}

</script>

</body>

</html>
"""


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print("")
    print("=" * 60)
    print("       SKILL ARENA HUB")
    print("=" * 60)
    print("")
    print("Website:")
    print("http://127.0.0.1:5000")
    print("")
    print("Admin:")
    print("http://127.0.0.1:5000/admin")
    print("")
    print("Default Admin Username:")
    print(ADMIN_USER)
    print("")
    print("Default Admin Password:")
    print(ADMIN_PASSWORD)
    print("")
    print("=" * 60)

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )