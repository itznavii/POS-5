from datetime import datetime
from decimal import Decimal
import json

from flask import Blueprint, current_app, render_template, request, jsonify, send_file, abort, flash, redirect, url_for
from flask_login import login_required, current_user

from app import db
from app.decorators import admin_required
from app.models import (
    Product,
    Sale,
    SaleItem,
    SalePayment,
    SaleRefund,
    SaleAttachment,
    RestaurantTable,
    Customer,
    InventoryTransaction,
)
from app.utils import (
    new_sale_number,
    new_queue_number,
    log_activity,
    deduct_inventory_for_sale_item,
    check_inventory_availability,
    check_low_stock_and_notify,
    generate_receipt_pdf,
    save_upload,
    to_money,
)
from app.notifications.helpers import notify

pos = Blueprint("pos", __name__)

SENIOR_PWD_RATE = Decimal("0.20")
ZERO = Decimal("0.00")
# Payment methods accepted by the POS. Any value outside this set is
# rejected server-side rather than trusted as-is.
VALID_PAYMENT_METHODS = {"cash", "gcash", "credit card"}
METHODS_REQUIRING_REFERENCE = {"gcash"}


@pos.route("/")
@login_required
def index():
    buffet_products = Product.query.filter_by(available=True, is_buffet=True).all()
    tables = RestaurantTable.query.order_by(RestaurantTable.name).all()
    preselect_table_id = request.args.get("table_id", type=int)
    return render_template(
        "pos/index.html", buffet_products=buffet_products, tables=tables, preselect_table_id=preselect_table_id
    )


@pos.route("/api/buffet-products")
@login_required
def api_buffet_products():
    products = Product.query.filter_by(available=True, is_buffet=True).all()
    results = [_product_json(p) for p in products]
    return jsonify(results)


def _product_json(p):
    tiers = {t.tier: float(t.price) for t in p.buffet_tiers}
    return {
        "id": p.id,
        "name": p.name,
        "is_buffet": p.is_buffet,
        "buffet_tiers": tiers,
    }


@pos.route("/api/next-queue-number")
@login_required
def api_next_queue():
    return jsonify({"queue_number": new_queue_number()})


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
@pos.route("/checkout", methods=["POST"])
@login_required
def checkout():
    # Accepts multipart/form-data (or regular form-encoded) so a
    # proof-of-payment file can be attached: field "payload" holds the
    # JSON-encoded sale data, field "proof_file" holds the optional/required
    # attachment. Falls back to a plain JSON body (no file) for compatibility.
    if "payload" in request.form:
        data = json.loads(request.form.get("payload", "{}"))
        proof_file = request.files.get("proof_file")
    else:
        data = request.get_json(force=True)
        proof_file = None

    product = Product.query.filter_by(id=data.get("product_id"), is_buffet=True).first()
    if not product:
        return jsonify({"error": "Select a valid buffet package"}), 400

    try:
        adult = int(data.get("adult", 0) or 0)
        senior = int(data.get("senior", 0) or 0)
        pwd = int(data.get("pwd", 0) or 0)
        kids = int(data.get("kids", 0) or 0)
        free = int(data.get("free", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Guest counts must be whole numbers."}), 400

    if any(x < 0 for x in (adult, senior, pwd, kids, free)):
        return jsonify({"error": "Guest counts cannot be negative."}), 400

    total_pax = adult + senior + pwd + kids + free
    if total_pax <= 0:
        return jsonify({"error": "Enter at least 1 guest"}), 400
    if total_pax > 500:
        return jsonify({"error": "Guest count looks too large for a single sale. Please split into multiple sales."}), 400

    adult_price = product.tier_price("adult") or ZERO
    senior_price = product.tier_price("senior") or ZERO
    pwd_price = product.tier_price("pwd") or ZERO
    kids_price = product.tier_price("kids") or ZERO

    # Guest-type totals, all Decimal. Senior/PWD pay their own (lower) tier
    # price - that price difference is savings/informational, not a second
    # "discount" applied on top (see Sale.senior_pwd_savings).
    adult_total = adult * adult_price
    senior_total = senior * senior_price
    pwd_total = pwd * pwd_price
    kids_total = kids * kids_price
    # free guests contribute 0

    subtotal = (adult_total + senior_total + pwd_total + kids_total).quantize(Decimal("0.01"))
    senior_pwd_savings = (
        (adult_price - senior_price) * senior + (adult_price - pwd_price) * pwd
    ).quantize(Decimal("0.01"))

    item = SaleItem(
        product_id=product.id,
        product_name=product.name,
        quantity=total_pax,
        price=adult_price,
        line_total=subtotal,
        is_buffet=True,
        buffet_adult=adult,
        buffet_senior=senior,
        buffet_pwd=pwd,
        buffet_kids=kids,
        buffet_free=free,
    )
    sale_items = [(item, product)]

    # Reject the sale up front if there isn't enough stock for what's being
    # ordered, instead of letting inventory silently go negative. Guests are
    # about to be served this food, so this has to be checked now - not
    # deferred until some later reconciliation.
    shortfalls = check_inventory_availability(sale_items)
    if shortfalls:
        details = "; ".join(
            f"{s['item'].name}: need {s['needed']:g} {s['item'].unit}, have {s['available']:g} {s['item'].unit}"
            for s in shortfalls
        )
        return jsonify({"error": f"Not enough stock to complete this sale ({details})."}), 400

    try:
        promo_percent = Decimal(str(data.get("promo_discount_percent", 0) or 0))
    except Exception:
        return jsonify({"error": "Invalid discount percentage."}), 400
    promo_percent = max(ZERO, min(Decimal("100"), promo_percent))
    promo_amount = (subtotal * promo_percent / Decimal("100")).quantize(Decimal("0.01"))

    # `discount` reflects ONLY the actual monetary reduction (promo/manual
    # discount) applied to the bill - senior/PWD pricing is never added back
    # in here, since it was never charged in the first place.
    total_discount = promo_amount
    taxable = max(ZERO, subtotal - total_discount)
    total = taxable

    payments = data.get("payments", [])
    if not isinstance(payments, list):
        return jsonify({"error": "Invalid payment data."}), 400

    try:
        payment_amounts = [to_money(p.get("amount", 0)) for p in payments]
    except ValueError:
        return jsonify({"error": "Invalid payment amount."}), 400

    if any(a < ZERO for a in payment_amounts):
        return jsonify({"error": "Payment amounts cannot be negative."}), 400

    amount_tendered = sum(payment_amounts, ZERO)

    is_walkin = bool(data.get("is_walkin", False))
    table_id = data.get("table_id") or None
    table = RestaurantTable.query.get(table_id) if table_id else None
    dining_session_id = table.session_id if (table and table.session_id) else None
    # An order placed against a table that has an open DiningSession is a
    # "tab" order: it goes to the kitchen and deducts inventory right away,
    # but doesn't need to be paid in full now - the balance is settled when
    # the whole session is closed (see tables_bp.close_session).
    is_tab = dining_session_id is not None

    if not is_tab and (not payments or amount_tendered <= ZERO):
        return jsonify({"error": "Please enter at least one payment before completing the sale."}), 400

    needs_proof = False
    for p in payments:
        method = (p.get("method") or "").strip()
        if method.lower() not in VALID_PAYMENT_METHODS:
            return jsonify({"error": f"'{method}' is not a recognized payment method."}), 400
        if method.lower() in METHODS_REQUIRING_REFERENCE:
            if not (p.get("reference_number") or "").strip():
                return jsonify({"error": f"A reference number is required for {method} payments."}), 400
            needs_proof = True

    if needs_proof and not (proof_file and proof_file.filename):
        return jsonify({"error": "Please attach proof of payment (screenshot/receipt) for GCash payments."}), 400

    if not is_tab and amount_tendered + Decimal("0.01") < total:
        return jsonify({"error": f"Payment (₱{amount_tendered:.2f}) is less than the total bill (₱{total:.2f})."}), 400

    attachment_filename = None
    if proof_file and proof_file.filename:
        try:
            attachment_filename = save_upload(proof_file, subfolder="sales")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    customer_id = data.get("customer_id") or None
    customer_name = data.get("customer_name") or ("Walk-in" if is_walkin else "Guest")
    reservation_id = data.get("reservation_id") or None

    # A discount needs admin approval if EITHER the peso amount is large OR
    # the percentage itself is large - a staff member can no longer dodge
    # approval simply by applying a huge % discount to a cheap order.
    approval_threshold = Decimal(str(current_app.config.get("LARGE_DISCOUNT_APPROVAL_THRESHOLD", 1000.0)))
    max_staff_percent = Decimal(str(current_app.config.get("MAX_STAFF_DISCOUNT_PERCENT", 10.0)))
    requires_approval = (
        not current_user.is_admin()
        and (total_discount > approval_threshold or promo_percent > max_staff_percent)
    )

    sale = Sale(
        sale_number=new_sale_number(),
        customer_id=customer_id,
        customer_name=customer_name,
        is_walkin=is_walkin,
        queue_number=new_queue_number() if is_walkin else None,
        table_id=table_id,
        dining_session_id=dining_session_id,
        is_tab=is_tab,
        reservation_id=reservation_id,
        subtotal=subtotal,
        discount=total_discount,
        discount_type="promo" if total_discount else "none",
        senior_pwd_savings=senior_pwd_savings,
        vat=ZERO,
        total=total,
        amount_tendered=amount_tendered,
        change=max(ZERO, amount_tendered - total),
        status="open" if requires_approval else "completed",
        requires_approval=requires_approval,
        cashier_id=current_user.id,
    )
    db.session.add(sale)
    db.session.flush()

    for item, product in sale_items:
        item.sale_id = sale.id
        db.session.add(item)
        if not requires_approval:
            deduct_inventory_for_sale_item(item, product)

    for p, amount in zip(payments, payment_amounts):
        db.session.add(
            SalePayment(
                sale_id=sale.id,
                method=p.get("method", "Cash"),
                amount=amount,
                reference_number=p.get("reference_number"),
            )
        )

    if attachment_filename:
        db.session.add(SaleAttachment(sale_id=sale.id, filename=attachment_filename))

    if table_id and not is_tab:
        # A table picked without an open session is the old "quick dine-in,
        # pay immediately" flow - mark it occupied directly. Tables that ARE
        # part of an open session are already occupied from open_table().
        table = RestaurantTable.query.get(table_id)
        if table:
            table.status = "occupied"

    db.session.commit()

    if requires_approval:
        notify(
            "discount_approval",
            f"Sale {sale.sale_number} needs approval: discount PHP {total_discount:.2f}",
            related_id=sale.id,
        )
        log_activity(f"Created sale {sale.sale_number} pending approval (discount PHP {total_discount:.2f})")
        return jsonify({"sale_id": sale.id, "requires_approval": True, "sale_number": sale.sale_number})

    check_low_stock_and_notify()
    log_activity(f"Completed sale {sale.sale_number} (PHP {total:.2f})")
    return jsonify({
        "sale_id": sale.id,
        "requires_approval": False,
        "sale_number": sale.sale_number,
        "total": float(total),
        "dining_session_id": dining_session_id,
    })


@pos.route("/approvals")
@login_required
@admin_required
def approvals():
    pending = Sale.query.filter_by(requires_approval=True, status="open").order_by(Sale.created_at).all()
    return render_template("pos/approvals.html", pending=pending)


@pos.route("/approve/<int:sale_id>", methods=["POST"])
@login_required
@admin_required
def approve_sale(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    sale.status = "completed"
    sale.approved_by_id = current_user.id
    for item in sale.items:
        deduct_inventory_for_sale_item(item, item_product(item))

    if sale.reservation_id and sale.reservation:
        sale.reservation.status = "Completed"
        sale.reservation.billing_requires_approval = False
        sale.reservation.billing_approved_by_id = current_user.id

    db.session.commit()
    check_low_stock_and_notify()
    log_activity(f"Approved sale {sale.sale_number}")
    flash(f"Sale {sale.sale_number} approved.", "success")
    return redirect(url_for("pos.approvals"))


def item_product(item):
    return Product.query.get(item.product_id)


@pos.route("/void/<int:sale_id>", methods=["POST"])
@login_required
@admin_required
def void_sale(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    reason = request.form.get("reason", "No reason given")
    if sale.status == "voided":
        flash("Sale already voided.", "warning")
        return redirect(request.referrer or url_for("main.dashboard"))

    # reverse inventory deductions tied to this sale
    txns = InventoryTransaction.query.filter_by(reference=str(sale.id), type="sale_deduction").all()
    for t in txns:
        if t.item:
            t.item.quantity = (t.item.quantity or 0) + t.quantity
            db.session.add(
                InventoryTransaction(
                    item_id=t.item.id,
                    type="void_reversal",
                    quantity=t.quantity,
                    reference=str(sale.id),
                    note=f"Void of sale {sale.sale_number}: {reason}",
                    user_id=current_user.id,
                )
            )

    # Financial reversal: record a full refund for whatever was actually
    # received, net of any refunds already issued, so payment reports stay
    # accurate instead of leaving stale SalePayment rows behind a "voided"
    # sale as if the money were still collected.
    refund_amount = sale.refundable_amount()
    if refund_amount > ZERO:
        db.session.add(
            SaleRefund(
                sale_id=sale.id,
                amount=refund_amount,
                reason=reason,
                refund_type="void",
                method="reversal",
                refunded_by_id=current_user.id,
            )
        )

    sale.status = "voided"
    sale.void_reason = reason
    sale.voided_by_id = current_user.id
    if sale.table_id and not sale.dining_session_id:
        # Only free the table for the old "quick dine-in, pay immediately"
        # flow (no dining session). A tab order belongs to a table's open
        # DiningSession alongside possibly other still-active orders -
        # voiding one shouldn't touch the table or close out the others.
        table = RestaurantTable.query.get(sale.table_id)
        if table:
            table.status = "cleaning"
    db.session.commit()
    log_activity(
        f"Voided sale {sale.sale_number}: {reason} "
        f"(refunded PHP {refund_amount:.2f}, old total PHP {sale.total:.2f})"
    )
    flash(f"Sale {sale.sale_number} voided, inventory restocked, and PHP {refund_amount:.2f} recorded as refunded.", "success")
    return redirect(request.referrer or url_for("main.dashboard"))


@pos.route("/refund/<int:sale_id>", methods=["POST"])
@login_required
@admin_required
def refund_sale(sale_id):
    """Partial refund - unlike void, this does NOT reverse inventory or
    change the sale's status, since the food/items were still served. It
    only records that some of the money collected was given back."""
    sale = Sale.query.get_or_404(sale_id)
    if sale.status == "voided":
        flash("This sale is already voided.", "warning")
        return redirect(url_for("pos.sale_detail", sale_id=sale.id))

    try:
        amount = to_money(request.form.get("amount"))
    except ValueError:
        flash("Enter a valid refund amount.", "danger")
        return redirect(url_for("pos.sale_detail", sale_id=sale.id))

    reason = (request.form.get("reason") or "").strip()
    method = request.form.get("method", "Cash")

    if amount <= ZERO:
        flash("Refund amount must be greater than zero.", "danger")
        return redirect(url_for("pos.sale_detail", sale_id=sale.id))
    if not reason:
        flash("Please provide a reason for the refund.", "danger")
        return redirect(url_for("pos.sale_detail", sale_id=sale.id))
    if amount > sale.refundable_amount():
        flash(
            f"Refund amount (PHP {amount:.2f}) exceeds what can still be refunded "
            f"(PHP {sale.refundable_amount():.2f}).",
            "danger",
        )
        return redirect(url_for("pos.sale_detail", sale_id=sale.id))

    db.session.add(
        SaleRefund(
            sale_id=sale.id,
            amount=amount,
            reason=reason,
            refund_type="partial",
            method=method,
            refunded_by_id=current_user.id,
        )
    )
    db.session.commit()
    log_activity(f"Refunded PHP {amount:.2f} on sale {sale.sale_number}: {reason}")
    flash(f"Refunded PHP {amount:.2f}.", "success")
    return redirect(url_for("pos.sale_detail", sale_id=sale.id))


@pos.route("/receipt/<int:sale_id>")
@login_required
def receipt(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    pdf = generate_receipt_pdf(sale)
    return send_file(pdf, mimetype="application/pdf", download_name=f"{sale.sale_number}.pdf")


@pos.route("/history")
@login_required
def history():
    q = (request.args.get("q") or "").strip()
    query = Sale.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Sale.sale_number.ilike(like), Sale.customer_name.ilike(like)))
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Sale.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template("pos/history.html", sales=pagination.items, pagination=pagination, q=q)


@pos.route("/sale/<int:sale_id>")
@login_required
def sale_detail(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    return render_template("pos/sale_detail.html", sale=sale)
