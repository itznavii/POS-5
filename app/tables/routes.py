from datetime import datetime
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from app import db
from app.models import RestaurantTable, DiningSession, Sale, SalePayment
from app.utils import log_activity, new_session_number, to_money
from app.decorators import admin_required

tables_bp = Blueprint("tables_bp", __name__)

ZERO = Decimal("0.00")


@tables_bp.route("/")
@login_required
def layout():
    tables = RestaurantTable.query.order_by(RestaurantTable.name).all()
    return render_template("tables/layout.html", tables=tables)


@tables_bp.route("/add", methods=["POST"])
@login_required
@admin_required
def add_table():
    name = request.form.get("name")
    capacity = request.form.get("capacity", 4)
    if name:
        db.session.add(RestaurantTable(name=name, capacity=capacity))
        db.session.commit()
        log_activity(f"Added table {name}")
        flash("Table added.", "success")
    return redirect(url_for("tables_bp.layout"))


@tables_bp.route("/<int:id>/status", methods=["POST"])
@login_required
def update_status(id):
    table = RestaurantTable.query.get_or_404(id)
    new_status = request.form.get("status") or (request.get_json(silent=True) or {}).get("status")
    if new_status in ("available", "occupied", "reserved", "cleaning"):
        # Freeing a table manually also detaches it from any session -
        # otherwise a stale session_id would make it look "open" again the
        # next time someone looks at session.tables.
        if new_status in ("available", "cleaning") and table.session_id:
            table.session_id = None
            table.merged_into_id = None
        table.status = new_status
        db.session.commit()
        log_activity(f"Table {table.name} set to {new_status}")
    if request.is_json:
        return jsonify({"ok": True, "status": table.status})
    return redirect(url_for("tables_bp.layout"))


# ---------------------------------------------------------------------------
# Dining sessions: TABLE(s) -> DiningSession -> Sale(s) -> kitchen -> payment
# ---------------------------------------------------------------------------
@tables_bp.route("/<int:id>/open", methods=["POST"])
@login_required
def open_table(id):
    table = RestaurantTable.query.get_or_404(id)
    if table.session_id:
        flash(f"{table.name} already has an open session.", "warning")
        return redirect(url_for("tables_bp.session_detail", session_id=table.session_id))
    if table.status not in ("available", "reserved"):
        flash(f"{table.name} is not available to seat.", "danger")
        return redirect(url_for("tables_bp.layout"))

    try:
        pax = int(request.form.get("pax", 1) or 1)
    except (TypeError, ValueError):
        pax = 1
    pax = max(1, pax)

    session_obj = DiningSession(
        session_number=new_session_number(),
        customer_name=request.form.get("customer_name") or "Walk-in",
        pax=pax,
        opened_by_id=current_user.id,
    )
    db.session.add(session_obj)
    db.session.flush()
    table.session_id = session_obj.id
    table.status = "occupied"
    db.session.commit()
    log_activity(f"Opened {table.name} ({session_obj.session_number}, {pax} pax)")
    flash(f"{table.name} is now open for {pax} guest(s).", "success")
    return redirect(url_for("tables_bp.session_detail", session_id=session_obj.id))


@tables_bp.route("/session/<int:session_id>")
@login_required
def session_detail(session_id):
    session_obj = DiningSession.query.get_or_404(session_id)
    return render_template("tables/session_detail.html", session=session_obj)


@tables_bp.route("/session/<int:session_id>/close", methods=["POST"])
@login_required
def close_session(session_id):
    session_obj = DiningSession.query.get_or_404(session_id)
    if session_obj.status == "closed":
        flash("This session is already closed.", "warning")
        return redirect(url_for("tables_bp.layout"))

    balance = session_obj.balance_due()
    if balance > ZERO:
        try:
            amount = to_money(request.form.get("final_payment_amount", 0))
        except ValueError:
            flash("Enter a valid payment amount.", "danger")
            return redirect(url_for("tables_bp.session_detail", session_id=session_id))
        method = request.form.get("final_payment_method", "Cash")
        if amount + Decimal("0.01") < balance:
            flash(
                f"Payment (PHP {amount:.2f}) doesn't cover the remaining balance "
                f"(PHP {balance:.2f}).",
                "danger",
            )
            return redirect(url_for("tables_bp.session_detail", session_id=session_id))
        # Apply the final payment against the most recent unpaid sale in the
        # session so every peso collected is still tied to a real Sale/
        # SalePayment record (keeps the payment ledger consistent).
        unpaid_sales = [s for s in session_obj.active_sales() if s.net_paid() < s.total]
        target_sale = unpaid_sales[-1] if unpaid_sales else (
            session_obj.active_sales()[-1] if session_obj.active_sales() else None
        )
        if target_sale:
            db.session.add(SalePayment(sale_id=target_sale.id, method=method, amount=amount))

    session_obj.status = "closed"
    session_obj.closed_at = datetime.utcnow()
    for t in session_obj.tables:
        t.session_id = None
        t.merged_into_id = None
        t.status = "cleaning"
    db.session.commit()
    log_activity(f"Closed session {session_obj.session_number} ({session_obj.table_names()})")
    flash(f"Session {session_obj.session_number} closed.", "success")
    return redirect(url_for("tables_bp.layout"))


@tables_bp.route("/<int:id>/transfer", methods=["POST"])
@login_required
def transfer(id):
    """Move an occupied table's whole open session (and every order/sale
    tied to it) to another table."""
    source = RestaurantTable.query.get_or_404(id)
    dest_id = request.form.get("dest_table_id")
    dest = RestaurantTable.query.get_or_404(dest_id)

    if dest.status != "available":
        flash("Destination table is not available.", "danger")
        return redirect(url_for("tables_bp.layout"))
    if not source.session_id:
        flash(f"{source.name} has no open session to transfer.", "warning")
        return redirect(url_for("tables_bp.layout"))

    dest.session_id = source.session_id
    dest.status = "occupied"
    source.session_id = None
    source.status = "cleaning"
    db.session.commit()
    log_activity(f"Transferred table {source.name} -> {dest.name}")
    flash(f"Transferred {source.name} to {dest.name}.", "success")
    return redirect(url_for("tables_bp.layout"))


@tables_bp.route("/merge", methods=["POST"])
@login_required
def merge():
    """Combine several tables under ONE shared DiningSession, so all of
    their orders/sales/payments are tracked together instead of each table
    just pointing a "merged_into" label at another table with nothing
    actually joined up."""
    ids = request.form.getlist("table_ids")
    if len(ids) < 2:
        flash("Select at least two tables to merge.", "warning")
        return redirect(url_for("tables_bp.layout"))

    tables = [RestaurantTable.query.get(tid) for tid in ids]
    tables = [t for t in tables if t]
    if len(tables) < 2:
        flash("Select at least two valid tables to merge.", "warning")
        return redirect(url_for("tables_bp.layout"))

    primary = tables[0]

    # Reuse the primary's open session if it has one; otherwise the first
    # table with an open session becomes the shared one; otherwise open a
    # fresh session for the whole group.
    session_obj = primary.session
    if not session_obj:
        session_obj = next((t.session for t in tables if t.session), None)
    if not session_obj:
        try:
            pax = sum(t.capacity or 0 for t in tables)
        except TypeError:
            pax = len(tables) * 4
        session_obj = DiningSession(
            session_number=new_session_number(),
            customer_name=request.form.get("customer_name") or "Walk-in",
            pax=pax or 1,
            opened_by_id=current_user.id,
        )
        db.session.add(session_obj)
        db.session.flush()

    for t in tables:
        # A table already tied to a DIFFERENT open session can't be silently
        # folded in - its own orders would need reconciling first.
        if t.session_id and t.session_id != session_obj.id:
            flash(
                f"{t.name} already has its own open session ({t.session.session_number}) - "
                "close or transfer it before merging.",
                "danger",
            )
            return redirect(url_for("tables_bp.layout"))
        t.session_id = session_obj.id
        t.status = "occupied"
        t.merged_into_id = primary.id if t.id != primary.id else None

    db.session.commit()
    log_activity(f"Merged tables {[t.name for t in tables]} into session {session_obj.session_number}")
    flash("Tables merged.", "success")
    return redirect(url_for("tables_bp.session_detail", session_id=session_obj.id))
