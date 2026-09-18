import os
import sys
import secrets as _secrets
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

# "production" unless explicitly told otherwise - fail-safe default so a
# missing/misconfigured FLASK_ENV doesn't silently run with dev fallbacks.
ENV = os.environ.get("FLASK_ENV", "production").lower()
IS_PRODUCTION = ENV == "production"

# Running under a test runner (pytest) shouldn't require production secrets.
IS_TESTING = "pytest" in sys.modules or os.environ.get("TESTING") == "1"


def _get_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if IS_PRODUCTION and not IS_TESTING:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. Refusing to start in "
            "production with a default/predictable secret key. Set SECRET_KEY "
            "to a long random value (e.g. `python -c \"import secrets; "
            "print(secrets.token_hex(32))\"`) or set FLASK_ENV=development for "
            "local work."
        )
    # Dev/test-only fallback - never used in production because of the check above.
    return "dev-only-insecure-key-" + _secrets.token_hex(8)


class Config:
    SECRET_KEY = _get_secret_key()
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "site.db")
    )
    # Render/Heroku style URLs sometimes come as postgres:// - SQLAlchemy needs postgresql://
    if SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace(
            "postgres://", "postgresql://", 1
        )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # IMPORTANT: kept OUTSIDE app/static so uploaded files (payment proofs,
    # which can contain names, phone numbers, bank/GCash details) are never
    # directly web-accessible. Served only via an authenticated route
    # (see app/main/routes.py: serve_upload).
    UPLOAD_FOLDER = os.environ.get(
        "UPLOAD_FOLDER", os.path.join(BASE_DIR, "instance", "uploads")
    )
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
    WTF_CSRF_ENABLED = True
    ALLOWED_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf", "webp"}

    # Business rules
    SENIOR_PWD_DISCOUNT_RATE = 0.20
    LARGE_DISCOUNT_APPROVAL_THRESHOLD = 1000.0  # PHP - flags for admin approval
    # Non-admin staff can apply at most this much discount without triggering
    # the approval workflow, regardless of the resulting peso amount. Closes
    # the loophole where a 100% discount on a cheap order needed no approval.
    MAX_STAFF_DISCOUNT_PERCENT = float(os.environ.get("MAX_STAFF_DISCOUNT_PERCENT", 10.0))

    # Login brute-force protection
    LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", 5))
    LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", 15))

    # First-run admin/staff account seeding (see app/seed_data.py). If these
    # are not set, run_seed() generates a random password and logs it once
    # instead of using a predictable default.
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
    SEED_DEMO_STAFF_ACCOUNT = os.environ.get("SEED_DEMO_STAFF_ACCOUNT", "0") == "1"
