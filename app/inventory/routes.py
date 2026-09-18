from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app import db
from app.decorators import role_required, admin_required
from app.models import InventoryItem, InventoryTransaction, Supplier
from app.utils import log_activity, check_low_stock_and_notify

inventory = Blueprint("inventory", __name__)


@inventory.route("/")
@login_required
@role_required("admin")
def list_items():
    items = InventoryItem.query.order_by(InventoryItem.name).all()
    return render_template("inventory/list.html", items=items)


@inventory.route("/add", methods=["POST"])
@login_required
@admin_required
def add_item():
    name = request.form.get("name")
    unit = request.form.get("unit", "pcs")
    try:
        qty = float(request.form.get("quantity", 0) or 0)
        threshold = float(request.form.get("low_stock_threshold", 5) or 5)
    except (TypeError, ValueError):
        flash("Quantity and low-stock threshold must be valid numbers.", "danger")
        return redirect(url_for("inventory.list_items"))
    if qty < 0 or threshold < 0:
        flash("Quantity and low-stock threshold cannot be negative.", "danger")
        return redirect(url_for("inventory.list_items"))
    supplier_id = request.form.get("supplier_id") or None
    if name:
        item = InventoryItem(
            name=name, unit=unit, quantity=qty, low_stock_threshold=threshold, supplier_id=supplier_id
        )
        db.session.add(item)
        db.session.commit()
        log_activity(f"Added inventory item {name}")
        flash("Inventory item added.", "success")
    return redirect(url_for("inventory.list_items"))


@inventory.route("/<int:id>/stock-in", methods=["POST"])
@login_required
@admin_required
def stock_in(id):
    item = InventoryItem.query.get_or_404(id)
    try:
        qty = float(request.form.get("quantity", 0) or 0)
    except (TypeError, ValueError):
        flash("Enter a valid quantity.", "danger")
        return redirect(url_for("inventory.list_items"))
    if qty <= 0:
        flash("Stock-in quantity must be greater than zero.", "danger")
        return redirect(url_for("inventory.list_items"))
    note = request.form.get("note", "")
    item.quantity = (item.quantity or 0) + qty
    db.session.add(InventoryTransaction(item_id=item.id, type="in", quantity=qty, note=note, user_id=current_user.id))
    db.session.commit()
    log_activity(f"Stock in: {item.name} +{qty}")
    flash("Stock added.", "success")
    return redirect(url_for("inventory.list_items"))


@inventory.route("/<int:id>/stock-out", methods=["POST"])
@login_required
@admin_required
def stock_out(id):
    item = InventoryItem.query.get_or_404(id)
    try:
        qty = float(request.form.get("quantity", 0) or 0)
    except (TypeError, ValueError):
        flash("Enter a valid quantity.", "danger")
        return redirect(url_for("inventory.list_items"))
    if qty <= 0:
        flash("Stock-out quantity must be greater than zero.", "danger")
        return redirect(url_for("inventory.list_items"))
    if qty > (item.quantity or 0):
        flash(f"Only {item.quantity:g} {item.unit} of {item.name} on hand.", "danger")
        return redirect(url_for("inventory.list_items"))
    note = request.form.get("note", "")
    item.quantity = (item.quantity or 0) - qty
    db.session.add(InventoryTransaction(item_id=item.id, type="out", quantity=qty, note=note, user_id=current_user.id))
    db.session.commit()
    check_low_stock_and_notify()
    log_activity(f"Stock out: {item.name} -{qty}")
    flash("Stock removed.", "success")
    return redirect(url_for("inventory.list_items"))


@inventory.route("/<int:id>/adjust", methods=["POST"])
@login_required
@admin_required
def adjust(id):
    item = InventoryItem.query.get_or_404(id)
    try:
        new_qty = float(request.form.get("new_quantity", item.quantity) or item.quantity)
    except (TypeError, ValueError):
        flash("Enter a valid quantity.", "danger")
        return redirect(url_for("inventory.list_items"))
    if new_qty < 0:
        flash("Quantity cannot be negative.", "danger")
        return redirect(url_for("inventory.list_items"))
    note = request.form.get("note", "Manual adjustment")
    diff = new_qty - (item.quantity or 0)
    item.quantity = new_qty
    db.session.add(InventoryTransaction(item_id=item.id, type="adjustment", quantity=diff, note=note, user_id=current_user.id))
    db.session.commit()
    check_low_stock_and_notify()
    log_activity(f"Adjusted {item.name} to {new_qty} ({note})")
    flash("Inventory adjusted.", "success")
    return redirect(url_for("inventory.list_items"))


@inventory.route("/<int:id>/waste", methods=["POST"])
@login_required
@admin_required
def waste(id):
    """Record spoiled/wasted/damaged stock - separate from a plain
    'stock-out' so reports can distinguish planned usage from loss."""
    item = InventoryItem.query.get_or_404(id)
    qty = float(request.form.get("quantity", 0) or 0)
    reason = (request.form.get("reason") or "").strip()
    waste_type = request.form.get("waste_type", "waste")
    if waste_type not in {"waste", "spoilage"}:
        waste_type = "waste"

    if qty <= 0:
        flash("Enter a quantity greater than zero.", "danger")
        return redirect(url_for("inventory.list_items"))
    if not reason:
        flash("Please provide a reason (e.g. spoiled, dropped, expired).", "danger")
        return redirect(url_for("inventory.list_items"))
    if qty > (item.quantity or 0):
        flash(
            f"Cannot record {qty:g} {item.unit} as {waste_type} - only {item.quantity:g} {item.unit} on hand.",
            "danger",
        )
        return redirect(url_for("inventory.list_items"))

    item.quantity = (item.quantity or 0) - qty
    db.session.add(
        InventoryTransaction(
            item_id=item.id,
            type=waste_type,
            quantity=qty,
            note=reason,
            user_id=current_user.id,
        )
    )
    db.session.commit()
    check_low_stock_and_notify()
    log_activity(f"Recorded {waste_type}: {item.name} -{qty:g} ({reason})")
    flash(f"Recorded {qty:g} {item.unit} of {item.name} as {waste_type}.", "success")
    return redirect(url_for("inventory.list_items"))


@inventory.route("/<int:id>/history")
@login_required
@admin_required
def history(id):
    item = InventoryItem.query.get_or_404(id)
    page = request.args.get("page", 1, type=int)
    pagination = (
        InventoryTransaction.query.filter_by(item_id=id)
        .order_by(InventoryTransaction.created_at.desc())
        .paginate(page=page, per_page=50, error_out=False)
    )
    return render_template("inventory/history.html", item=item, txns=pagination.items, pagination=pagination)


@inventory.route("/low-stock")
@login_required
@admin_required
def low_stock():
    items = InventoryItem.query.filter(InventoryItem.quantity <= InventoryItem.low_stock_threshold).all()
    return render_template("inventory/low_stock.html", items=items)


@inventory.route("/suppliers", methods=["GET", "POST"])
@login_required
@admin_required
def suppliers():
    if request.method == "POST":
        name = request.form.get("name")
        contact = request.form.get("contact")
        if name:
            db.session.add(Supplier(name=name, contact=contact))
            db.session.commit()
            flash("Supplier added.", "success")
        return redirect(url_for("inventory.suppliers"))
    items = Supplier.query.order_by(Supplier.name).all()
    return render_template("inventory/suppliers.html", items=items)
