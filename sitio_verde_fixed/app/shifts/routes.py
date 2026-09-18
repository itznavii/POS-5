from datetime import datetime
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app import db
from app.decorators import admin_required
from app.models import CashierShift, User
from app.utils import log_activity, to_money

shifts = Blueprint("shifts", __name__)

ZERO = Decimal("0.00")


def current_open_shift(user_id=None):
    return CashierShift.query.filter_by(
        user_id=user_id or current_user.id, status="open"
    ).first()


@shifts.route("/")
@login_required
def my_shift():
    """The cashier's own till: open one, see running totals, or close out."""
    shift = current_open_shift()
    page = request.args.get("page", 1, type=int)
    history = (
        CashierShift.query.filter_by(user_id=current_user.id, status="closed")
        .order_by(CashierShift.closed_at.desc())
        .paginate(page=page, per_page=10, error_out=False)
    )
    return render_template(
        "shifts/my_shift.html", shift=shift, history=history.items, pagination=history
    )


@shifts.route("/open", methods=["POST"])
@login_required
def open_shift():
    if current_open_shift():
        flash("You already have an open shift.", "warning")
        return redirect(url_for("shifts.my_shift"))

    try:
        opening_cash = to_money(request.form.get("opening_cash", 0))
    except ValueError:
        flash("Enter a valid opening cash amount.", "danger")
        return redirect(url_for("shifts.my_shift"))
    if opening_cash < ZERO:
        flash("Opening cash cannot be negative.", "danger")
        return redirect(url_for("shifts.my_shift"))

    shift = CashierShift(
        user_id=current_user.id, opening_cash=opening_cash, status="open"
    )
    db.session.add(shift)
    db.session.commit()
    log_activity(f"Opened cashier shift with PHP {opening_cash:.2f} float")
    flash(f"Shift opened with a PHP {opening_cash:.2f} cash float.", "success")
    return redirect(url_for("shifts.my_shift"))


@shifts.route("/<int:id>/close", methods=["POST"])
@login_required
def close_shift(id):
    shift = CashierShift.query.get_or_404(id)
    # A cashier closes their own till; an admin may close anyone's.
    if shift.user_id != current_user.id and not current_user.is_admin():
        flash("You can only close your own shift.", "danger")
        return redirect(url_for("shifts.my_shift"))
    if shift.status == "closed":
        flash("That shift is already closed.", "warning")
        return redirect(url_for("shifts.my_shift"))

    try:
        counted = to_money(request.form.get("closing_cash_counted", 0))
    except ValueError:
        flash("Enter a valid counted cash amount.", "danger")
        return redirect(url_for("shifts.my_shift"))
    if counted < ZERO:
        flash("Counted cash cannot be negative.", "danger")
        return redirect(url_for("shifts.my_shift"))

    # Capture the expected figure BEFORE setting closed_at, since closed_at
    # defines the end of the shift's sales window.
    expected = shift.expected_cash()
    shift.closing_cash_counted = counted
    shift.notes = (request.form.get("notes") or "").strip() or None
    shift.status = "closed"
    shift.closed_at = datetime.utcnow()
    db.session.commit()

    variance = counted - expected
    log_activity(
        f"Closed cashier shift #{shift.id}: expected PHP {expected:.2f}, "
        f"counted PHP {counted:.2f}, variance PHP {variance:.2f}"
    )
    if variance == ZERO:
        flash(f"Shift closed. Till balanced exactly at PHP {counted:.2f}.", "success")
    else:
        word = "over" if variance > ZERO else "short"
        flash(
            f"Shift closed. Till is PHP {abs(variance):.2f} {word} "
            f"(expected PHP {expected:.2f}, counted PHP {counted:.2f}).",
            "warning",
        )
    return redirect(url_for("shifts.detail", id=shift.id))


@shifts.route("/<int:id>")
@login_required
def detail(id):
    shift = CashierShift.query.get_or_404(id)
    if shift.user_id != current_user.id and not current_user.is_admin():
        flash("You can only view your own shifts.", "danger")
        return redirect(url_for("shifts.my_shift"))
    return render_template("shifts/detail.html", shift=shift)


@shifts.route("/<int:id>/approve", methods=["POST"])
@login_required
@admin_required
def approve(id):
    shift = CashierShift.query.get_or_404(id)
    if shift.status != "closed":
        flash("Only a closed shift can be approved.", "warning")
        return redirect(url_for("shifts.detail", id=id))
    shift.approved_by_id = current_user.id
    db.session.commit()
    variance = shift.variance() or ZERO
    log_activity(f"Approved cashier shift #{shift.id} (variance PHP {variance:.2f})")
    flash("Shift closing approved.", "success")
    return redirect(url_for("shifts.detail", id=id))


@shifts.route("/all")
@login_required
@admin_required
def all_shifts():
    page = request.args.get("page", 1, type=int)
    pagination = CashierShift.query.order_by(CashierShift.opened_at.desc()).paginate(
        page=page, per_page=25, error_out=False
    )
    return render_template(
        "shifts/all.html", items=pagination.items, pagination=pagination
    )
