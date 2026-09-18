from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app import db
from app.decorators import admin_required
from app.models import Expense
from app.utils import log_activity, to_money

expenses = Blueprint("expenses", __name__)

CATEGORIES = ["Utilities", "Salary", "Rent", "Marketing", "Supplies", "Others"]


@expenses.route("/")
@login_required
@admin_required
def list_expenses():
    items = Expense.query.order_by(Expense.date.desc()).all()
    total = sum((e.amount for e in items if e.amount), 0)
    return render_template("expenses/list.html", items=items, total=total, categories=CATEGORIES)


@expenses.route("/add", methods=["POST"])
@login_required
@admin_required
def add_expense():
    date_str = request.form.get("date")
    expense_date = (
        datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else datetime.utcnow().date()
    )
    try:
        amount = to_money(request.form.get("amount", 0))
    except ValueError:
        flash("Enter a valid expense amount.", "danger")
        return redirect(url_for("expenses.list_expenses"))
    if amount < 0:
        flash("Expense amount cannot be negative.", "danger")
        return redirect(url_for("expenses.list_expenses"))

    e = Expense(
        category=request.form.get("category"),
        description=request.form.get("description"),
        amount=amount,
        date=expense_date,
        recorded_by=current_user.id,
    )
    db.session.add(e)
    db.session.commit()
    log_activity(f"Added expense: {e.category} PHP {e.amount:.2f}")
    flash("Expense recorded.", "success")
    return redirect(url_for("expenses.list_expenses"))


@expenses.route("/<int:id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_expense(id):
    e = Expense.query.get_or_404(id)
    db.session.delete(e)
    db.session.commit()
    log_activity(f"Deleted expense #{id}")
    flash("Expense deleted.", "success")
    return redirect(url_for("expenses.list_expenses"))
