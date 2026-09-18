from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, current_app, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash

from app import db
from app.models import User
from app.utils import log_activity

auth = Blueprint("auth", __name__)


def _is_safe_redirect(target):
    """Only allow redirecting to a same-origin relative path, to prevent
    open-redirect abuse via ?next=https://evil.example."""
    if not target:
        return False
    parsed = urlparse(target)
    # No scheme and no netloc means it's a relative path on this site.
    return parsed.scheme == "" and parsed.netloc == "" and target.startswith("/")


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        now = datetime.utcnow()
        if user and user.locked_until and user.locked_until > now:
            wait_minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
            flash(
                f"Too many failed login attempts. Try again in about {wait_minutes} minute(s).",
                "danger",
            )
            return render_template("login.html")

        if user and user.active and check_password_hash(user.password_hash, password):
            user.failed_login_attempts = 0
            user.locked_until = None
            db.session.commit()
            login_user(user)
            log_activity(f"Logged in ({user.role})")

            if user.must_change_password:
                flash("Please set a new password to continue.", "warning")
                return redirect(url_for("auth.change_password"))

            flash("Logged in successfully!", "success")
            next_page = request.args.get("next")
            if _is_safe_redirect(next_page):
                return redirect(next_page)
            return redirect(url_for("main.dashboard"))

        # Invalid credentials: track attempts against the account (not just
        # by IP) so a locked-out account can't be brute-forced from anywhere.
        if user:
            max_attempts = current_app.config.get("LOGIN_MAX_ATTEMPTS", 5)
            lockout_minutes = current_app.config.get("LOGIN_LOCKOUT_MINUTES", 15)
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= max_attempts:
                user.locked_until = now + timedelta(minutes=lockout_minutes)
                user.failed_login_attempts = 0
                log_activity(f"Account locked after repeated failed logins: {username}")
            db.session.commit()

        flash("Invalid credentials or account disabled.", "danger")
    return render_template("login.html")


@auth.route("/logout")
@login_required
def logout():
    log_activity("Logged out")
    logout_user()
    return redirect(url_for("auth.login"))


@auth.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(current_user.password_hash, current_password):
            flash("Current password is incorrect.", "danger")
            return render_template("change_password.html")
        if len(new_password) < 10:
            flash("New password must be at least 10 characters.", "danger")
            return render_template("change_password.html")
        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return render_template("change_password.html")
        if new_password == current_password:
            flash("New password must be different from the current password.", "danger")
            return render_template("change_password.html")

        current_user.password_hash = generate_password_hash(new_password)
        current_user.must_change_password = False
        db.session.commit()
        log_activity("Changed own password")
        flash("Password updated.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("change_password.html")
