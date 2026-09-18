from datetime import datetime
from decimal import Decimal

from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, jsonify, send_file, abort
from flask_login import login_required, current_user

from app import db
from app.decorators import admin_required
from app.models import (
    Reservation,
    ReservationAttachment,
    ReservationPayment,
    Customer,
    Sale,
    SaleItem,
    SalePayment,
)
from app.utils import (
    new_reservation_number,
    new_sale_number,
    save_upload,
    log_activity,
    generate_receipt_pdf,
    to_money,
)
from app.notifications.helpers import notify

reservations = Blueprint("reservations", __name__)

ZERO = Decimal("0.00")
VALID_RESERVATION_PAYMENT_METHODS = {"cash", "gcash", "bank transfer", "card", "credit card"}
METHODS_REQUIRING_REFERENCE = {"gcash", "bank transfer"}

# Reservation status state machine: maps a status to the set of statuses it
# may transition into. Anything not listed as a valid "from" state for a
# given action is rejected, so e.g. a Cancelled reservation can't be
# Checked In, and a Completed one can't be re-Confirmed.
RESERVATION_TRANSITIONS = {
    "confirm": {"from": {"Reserved"}, "to": "Confirmed"},
    "cancel": {"from": {"Reserved", "Confirmed"}, "to": "Cancelled"},
    "no_show": {"from": {"Reserved", "Confirmed"}, "to": "No Show"},
    "checkin": {"from": {"Confirmed"}, "to": "Checked In"},
    "complete": {"from": {"Checked In"}, "to": "Completed"},
}


def _apply_transition(r, action):
    """Validate and describe a reservation status transition. Returns None if
    valid, or an error message string if the transition isn't allowed."""
    rule = RESERVATION_TRANSITIONS[action]
    if r.status not in rule["from"]:
        allowed = ", ".join(sorted(rule["from"]))
        return f"Cannot {action.replace('_', '-')} a reservation that is currently '{r.status}' (must be: {allowed})."
    return None


@reservations.route("/")
@login_required
def list_reservations():
    status = request.args.get("status")
    q = (request.args.get("q") or "").strip()
    query = Reservation.query
    if status:
        query = query.filter_by(status=status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Reservation.customer_name.ilike(like), Reservation.reservation_number.ilike(like), Reservation.phone.ilike(like))
        )
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Reservation.date.desc(), Reservation.time.desc()).paginate(
        page=page, per_page=25, error_out=False
    )
    return render_template("reservations/list.html", items=pagination.items, pagination=pagination, status=status, q=q)


@reservations.route("/calendar")
@login_required
def calendar_view():
    return render_template("reservations/calendar.html")


@reservations.route("/api/calendar-events")
@login_required
def calendar_events():
    color_map = {
        "Reserved": "#f0ad4e",
        "Confirmed": "#2f7a4f",
        "Checked In": "#0d6efd",
        "Completed": "#198754",
        "Cancelled": "#6c757d",
        "No Show": "#dc3545",
    }
    items = Reservation.query.all()
    events = []
    for r in items:
        events.append(
            {
                "id": r.id,
                "title": f"{r.customer_name} ({r.pax}pax) - {r.event_type or ''}",
                "start": f"{r.date.isoformat()}T{r.time.strftime('%H:%M:%S')}",
                "color": color_map.get(r.status, "#999"),
                "url": url_for("reservations.detail", id=r.id),
            }
        )
    return jsonify(events)


@reservations.route("/new", methods=["GET", "POST"])
@login_required
def new_reservation():
    if request.method == "POST":
        customer_name = request.form.get("customer_name")
        phone = request.form.get("phone")
        email = request.form.get("email")
        date_str = request.form.get("date")
        time_str = request.form.get("time")
        try:
            pax = int(request.form.get("pax", 1) or 1)
            dp_amount = to_money(request.form.get("down_payment", 0))
        except (TypeError, ValueError):
            flash("Guest count and down payment must be valid numbers.", "danger")
            return render_template("reservations/new.html")
        if pax < 1:
            flash("Guest count must be at least 1.", "danger")
            return render_template("reservations/new.html")
        if dp_amount < ZERO:
            flash("Down payment cannot be negative.", "danger")
            return render_template("reservations/new.html")
        payment_method = request.form.get("payment_method", "Cash")
        if dp_amount > 0 and payment_method.lower() not in VALID_RESERVATION_PAYMENT_METHODS:
            flash(f"'{payment_method}' is not a recognized payment method.", "danger")
            return render_template("reservations/new.html")

        customer = Customer.query.filter_by(phone=phone).first() if phone else None
        if not customer and customer_name:
            customer = Customer(name=customer_name, phone=phone, email=email)
            db.session.add(customer)
            db.session.flush()

        r = Reservation(
            reservation_number=new_reservation_number(),
            inquiry_number=request.form.get("inquiry_number"),
            customer_id=customer.id if customer else None,
            date=datetime.strptime(date_str, "%Y-%m-%d").date(),
            time=datetime.strptime(time_str, "%H:%M").time(),
            customer_name=customer_name,
            phone=phone,
            email=email,
            pax=pax,
            event_type=request.form.get("event_type"),
            special_requests=request.form.get("special_requests"),
            assigned_staff=request.form.get("assigned_staff"),
            down_payment=dp_amount,
            status="Reserved",
        )
        db.session.add(r)
        db.session.flush()

        # Optional down payment record + proof upload
        if dp_amount > 0:
            db.session.add(
                ReservationPayment(
                    reservation_id=r.id,
                    payment_type="down_payment",
                    method=payment_method,
                    amount=dp_amount,
                    reference_number=request.form.get("reference_number"),
                    status="Pending",
                )
            )

        proof_file = request.files.get("proof_of_payment")
        if proof_file and proof_file.filename:
            try:
                filename = save_upload(proof_file, subfolder="reservations")
                if filename:
                    db.session.add(ReservationAttachment(reservation_id=r.id, filename=filename))
            except ValueError as e:
                flash(str(e), "danger")

        db.session.commit()
        notify("new_reservation", f"New reservation {r.reservation_number} by {r.customer_name}", related_id=r.id)
        log_activity(f"Created reservation {r.reservation_number}")
        flash(f"Reservation {r.reservation_number} created.", "success")
        return redirect(url_for("reservations.detail", id=r.id))

    return render_template("reservations/new.html")


@reservations.route("/<int:id>")
@login_required
def detail(id):
    r = Reservation.query.get_or_404(id)
    return render_template("reservations/detail.html", r=r)


@reservations.route("/<int:id>/confirm", methods=["POST"])
@login_required
def confirm(id):
    r = Reservation.query.get_or_404(id)
    error = _apply_transition(r, "confirm")
    if error:
        flash(error, "danger")
        return redirect(url_for("reservations.detail", id=id))
    r.status = "Confirmed"
    r.confirmed_by = current_user.name or current_user.username
    r.date_confirmed = datetime.utcnow()
    db.session.commit()
    log_activity(f"Confirmed reservation {r.reservation_number}")
    flash("Reservation confirmed.", "success")
    return redirect(url_for("reservations.detail", id=id))


@reservations.route("/<int:id>/cancel", methods=["POST"])
@login_required
def cancel(id):
    r = Reservation.query.get_or_404(id)
    error = _apply_transition(r, "cancel")
    if error:
        flash(error, "danger")
        return redirect(url_for("reservations.detail", id=id))
    r.status = "Cancelled"
    db.session.commit()
    notify("cancelled_reservation", f"Reservation {r.reservation_number} was cancelled", related_id=r.id)
    log_activity(f"Cancelled reservation {r.reservation_number}")
    flash("Reservation cancelled.", "warning")
    return redirect(url_for("reservations.detail", id=id))


@reservations.route("/<int:id>/no-show", methods=["POST"])
@login_required
def no_show(id):
    r = Reservation.query.get_or_404(id)
    error = _apply_transition(r, "no_show")
    if error:
        flash(error, "danger")
        return redirect(url_for("reservations.detail", id=id))
    r.status = "No Show"
    db.session.commit()
    log_activity(f"Marked reservation {r.reservation_number} as No Show")
    flash("Reservation marked as No Show.", "warning")
    return redirect(url_for("reservations.detail", id=id))


@reservations.route("/<int:id>/upload-proof", methods=["POST"])
@login_required
def upload_proof(id):
    r = Reservation.query.get_or_404(id)
    proof_file = request.files.get("proof_of_payment")
    if proof_file and proof_file.filename:
        try:
            filename = save_upload(proof_file, subfolder="reservations")
            if filename:
                db.session.add(ReservationAttachment(reservation_id=r.id, filename=filename))
                db.session.commit()
                flash("Proof of payment uploaded.", "success")
        except ValueError as e:
            flash(str(e), "danger")
    return redirect(url_for("reservations.detail", id=id))


@reservations.route("/payment/<int:payment_id>/verify", methods=["POST"])
@login_required
@admin_required
def verify_payment(payment_id):
    payment = ReservationPayment.query.get_or_404(payment_id)
    action = request.form.get("action", "Verified")
    if action not in {"Verified", "Rejected"}:
        abort(400)
    payment.status = action
    payment.verified_by = current_user.name or current_user.username
    db.session.commit()
    log_activity(f"{action} payment #{payment.id} for reservation {payment.reservation.reservation_number}")
    flash(f"Payment {action.lower()}.", "success")
    return redirect(url_for("reservations.detail", id=payment.reservation_id))


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------
@reservations.route("/<int:id>/checkin", methods=["POST"])
@login_required
def checkin(id):
    r = Reservation.query.get_or_404(id)
    error = _apply_transition(r, "checkin")
    if error:
        flash(error, "danger")
        return redirect(url_for("reservations.detail", id=id))
    try:
        actual_pax = int(request.form.get("actual_pax", r.pax) or r.pax)
    except (TypeError, ValueError):
        flash("Actual guest count must be a whole number.", "danger")
        return redirect(url_for("reservations.detail", id=id))
    if actual_pax < 1:
        flash("Actual guest count must be at least 1.", "danger")
        return redirect(url_for("reservations.detail", id=id))
    r.status = "Checked In"
    r.arrival_time = datetime.utcnow()
    r.actual_pax = actual_pax
    db.session.commit()
    log_activity(f"Checked in reservation {r.reservation_number} ({r.actual_pax} guests)")
    flash("Guest checked in.", "success")
    return redirect(url_for("reservations.detail", id=id))


# ---------------------------------------------------------------------------
# Final billing: total bill entry -> auto-compute balance -> final payment ->
# generate official receipt -> mark Completed
# ---------------------------------------------------------------------------
@reservations.route("/<int:id>/final-billing", methods=["GET", "POST"])
@login_required
def final_billing(id):
    r = Reservation.query.get_or_404(id)

    if request.method == "POST":
        error = _apply_transition(r, "complete")
        if error:
            flash(error, "danger")
            return redirect(url_for("reservations.detail", id=id))

        try:
            total_bill = to_money(request.form.get("total_bill", 0))
            amount_paid = to_money(request.form.get("amount_paid", 0))
        except ValueError:
            flash("Total bill and amount paid must be valid numbers.", "danger")
            return redirect(url_for("reservations.final_billing", id=id))

        if total_bill < ZERO or amount_paid < ZERO:
            flash("Total bill and amount paid cannot be negative.", "danger")
            return redirect(url_for("reservations.final_billing", id=id))

        guests = r.actual_pax or r.pax or 1

        # Guard against an implausibly low total bill (e.g. a typo, or a
        # staff member giving away the meal). The system doesn't dictate
        # exact event pricing - banquet/event rates are legitimately
        # negotiated - but a non-admin submitting well below the standard
        # per-head rate needs a manager to confirm it before it's final.
        from app.models import Product
        standard_product = Product.query.filter_by(is_buffet=True, available=True).first()
        standard_adult_price = None
        if standard_product:
            standard_adult_price = standard_product.tier_price("adult")
        requires_billing_approval = False
        if (
            standard_adult_price
            and not current_user.is_admin()
            and total_bill < (standard_adult_price * guests * Decimal("0.5"))
        ):
            requires_billing_approval = True

        verified_dp = r.total_verified_paid()
        remaining = max(ZERO, total_bill - verified_dp)

        payment_method = request.form.get("payment_method", "Cash")
        reference_number = (request.form.get("reference_number") or "").strip()

        if payment_method.lower() not in VALID_RESERVATION_PAYMENT_METHODS and payment_method.lower() != "mixed payment":
            flash(f"'{payment_method}' is not a recognized payment method.", "danger")
            return redirect(url_for("reservations.final_billing", id=id))

        if payment_method.lower() in METHODS_REQUIRING_REFERENCE and not reference_number:
            flash(f"A reference number is required for {payment_method} payments.", "danger")
            return redirect(url_for("reservations.final_billing", id=id))

        if not requires_billing_approval and amount_paid + Decimal("0.01") < remaining:
            flash(
                f"Amount paid (PHP {amount_paid:.2f}) is less than the remaining balance "
                f"(PHP {remaining:.2f}).",
                "danger",
            )
            return redirect(url_for("reservations.final_billing", id=id))

        r.total_bill = total_bill
        r.remaining_balance = max(ZERO, remaining - amount_paid)
        r.billing_requires_approval = requires_billing_approval

        db.session.add(
            ReservationPayment(
                reservation_id=r.id,
                payment_type="final_payment",
                method=payment_method,
                amount=amount_paid,
                reference_number=request.form.get("reference_number"),
                status="Verified",
                verified_by=current_user.name or current_user.username,
            )
        )

        # Build a Sale record so it flows into reports/receipt like any
        # other transaction.
        sale = Sale(
            sale_number=new_sale_number(),
            customer_id=r.customer_id,
            customer_name=r.customer_name,
            is_walkin=False,
            reservation_id=r.id,
            subtotal=total_bill,
            discount=ZERO,
            discount_type="none",
            vat=ZERO,
            total=total_bill,
            amount_tendered=verified_dp + amount_paid,
            change=max(ZERO, (verified_dp + amount_paid) - total_bill),
            status="open" if requires_billing_approval else "completed",
            requires_approval=requires_billing_approval,
            cashier_id=current_user.id,
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(
            SaleItem(
                sale_id=sale.id,
                product_name=f"Reservation Buffet ({guests} guests)",
                quantity=guests,
                price=(total_bill / guests).quantize(Decimal("0.01")),
                line_total=total_bill,
            )
        )
        db.session.add(
            SalePayment(sale_id=sale.id, method=payment_method, amount=amount_paid, reference_number=request.form.get("reference_number"))
        )
        if verified_dp:
            db.session.add(SalePayment(sale_id=sale.id, method="Down Payment", amount=verified_dp))

        if requires_billing_approval:
            # Leave the reservation at "Checked In" - it only becomes
            # Completed once an admin approves the unusually low total.
            db.session.commit()
            notify(
                "discount_approval",
                f"Reservation {r.reservation_number} final bill (PHP {total_bill:.2f}) needs admin approval",
                related_id=sale.id,
            )
            log_activity(
                f"Final billing for reservation {r.reservation_number} pending approval "
                f"(total PHP {total_bill:.2f} looks low for {guests} guests)"
            )
            flash(
                "This total looks unusually low for the guest count and needs a manager's "
                "approval before the reservation can be marked Completed.",
                "warning",
            )
            return redirect(url_for("reservations.detail", id=id))

        r.status = "Completed"
        db.session.commit()
        log_activity(f"Final billing for reservation {r.reservation_number}: total PHP {total_bill:.2f}")
        flash("Final billing recorded. Reservation marked Completed.", "success")
        return redirect(url_for("pos.receipt", sale_id=sale.id))

    verified_dp = r.total_verified_paid()
    return render_template("reservations/final_billing.html", r=r, verified_dp=verified_dp)
