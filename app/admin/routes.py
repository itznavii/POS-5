import json
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from app import db
from app.decorators import admin_required
from app.utils import to_money
from app.models import (
    User,
    Product,
    Category,
    BuffetTier,
    Recipe,
    ActivityLog,
    Setting,
    InventoryItem,
    Sale,
    Reservation,
    Expense,
)
from app.utils import log_activity, save_upload

admin = Blueprint("admin", __name__)


# ---------------------------------------------------------------------------
# Staff / user management
# ---------------------------------------------------------------------------
@admin.route("/users")
@login_required
@admin_required
def users():
    items = User.query.order_by(User.username).all()
    return render_template("admin/users.html", items=items)


@admin.route("/users/add", methods=["POST"])
@login_required
@admin_required
def add_user():
    username = request.form.get("username", "").strip()
    if User.query.filter_by(username=username).first():
        flash("Username already exists.", "danger")
        return redirect(url_for("admin.users"))
    user = User(
        username=username,
        name=request.form.get("name"),
        role=request.form.get("role", "staff"),
        password_hash=generate_password_hash(request.form.get("password", "changeme123")),
    )
    db.session.add(user)
    db.session.commit()
    log_activity(f"Created user {username} ({user.role})")
    flash("Staff account created.", "success")
    return redirect(url_for("admin.users"))


@admin.route("/users/<int:id>/reset-password", methods=["POST"])
@login_required
@admin_required
def reset_password(id):
    user = User.query.get_or_404(id)
    new_password = request.form.get("password", "changeme123")
    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    log_activity(f"Reset password for {user.username}")
    flash(f"Password reset for {user.username}.", "success")
    return redirect(url_for("admin.users"))


@admin.route("/users/<int:id>/toggle-active", methods=["POST"])
@login_required
@admin_required
def toggle_active(id):
    user = User.query.get_or_404(id)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("admin.users"))
    user.active = not user.active
    db.session.commit()
    log_activity(f"{'Activated' if user.active else 'Deactivated'} user {user.username}")
    flash(f"{user.username} is now {'active' if user.active else 'deactivated'}.", "success")
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Menu / product management
# ---------------------------------------------------------------------------
@admin.route("/products")
@login_required
@admin_required
def products():
    items = Product.query.order_by(Product.name).all()
    categories = Category.query.all()
    inv_items = InventoryItem.query.all()
    return render_template("admin/products.html", items=items, categories=categories, inv_items=inv_items)


@admin.route("/products/add", methods=["POST"])
@login_required
@admin_required
def add_product():
    name = request.form.get("name")
    if not name:
        flash("Product name is required.", "danger")
        return redirect(url_for("admin.products"))

    image_file = request.files.get("image")
    image_name = None
    if image_file and image_file.filename:
        try:
            image_name = save_upload(image_file, subfolder="products")
        except ValueError as e:
            flash(str(e), "danger")

    is_buffet = request.form.get("is_buffet") == "on"
    try:
        selling_price = to_money(request.form.get("selling_price", 0))
        cost_price = to_money(request.form.get("cost_price", 0))
    except ValueError:
        flash("Selling price and cost price must be valid amounts.", "danger")
        return redirect(url_for("admin.products"))
    if selling_price < 0 or cost_price < 0:
        flash("Prices cannot be negative.", "danger")
        return redirect(url_for("admin.products"))

    p = Product(
        name=name,
        category_id=request.form.get("category_id") or None,
        selling_price=selling_price,
        cost_price=cost_price,
        description=request.form.get("description"),
        barcode=request.form.get("barcode") or None,
        image=image_name,
        is_buffet=is_buffet,
        inventory_item_id=request.form.get("inventory_item_id") or None,
        deduct_qty=float(request.form.get("deduct_qty", 1) or 1),
    )
    db.session.add(p)
    db.session.flush()

    if is_buffet:
        for tier in ["adult", "senior", "pwd", "kids", "free"]:
            try:
                price = to_money(request.form.get(f"tier_{tier}", 0))
            except ValueError:
                price = to_money(0)
            if price < 0:
                price = to_money(0)
            db.session.add(BuffetTier(product_id=p.id, tier=tier, price=price))

    db.session.commit()
    log_activity(f"Added product {name}")
    flash("Product added.", "success")
    return redirect(url_for("admin.products"))


@admin.route("/products/<int:id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_product(id):
    p = Product.query.get_or_404(id)
    p.name = request.form.get("name", p.name)
    p.category_id = request.form.get("category_id") or None
    try:
        selling_price = to_money(request.form.get("selling_price", p.selling_price))
        cost_price = to_money(request.form.get("cost_price", p.cost_price))
    except ValueError:
        flash("Selling price and cost price must be valid amounts.", "danger")
        return redirect(url_for("admin.products"))
    if selling_price < 0 or cost_price < 0:
        flash("Prices cannot be negative.", "danger")
        return redirect(url_for("admin.products"))
    p.selling_price = selling_price
    p.cost_price = cost_price
    p.description = request.form.get("description")
    p.barcode = request.form.get("barcode") or None
    p.available = request.form.get("available") == "on"
    p.inventory_item_id = request.form.get("inventory_item_id") or None
    p.deduct_qty = float(request.form.get("deduct_qty", 1) or 1)

    image_file = request.files.get("image")
    if image_file and image_file.filename:
        try:
            p.image = save_upload(image_file, subfolder="products")
        except ValueError as e:
            flash(str(e), "danger")

    if p.is_buffet:
        for tier in ["adult", "senior", "pwd", "kids", "free"]:
            try:
                price = to_money(request.form.get(f"tier_{tier}", 0))
            except ValueError:
                price = to_money(0)
            if price < 0:
                price = to_money(0)
            existing = next((t for t in p.buffet_tiers if t.tier == tier), None)
            if existing:
                existing.price = price
            else:
                db.session.add(BuffetTier(product_id=p.id, tier=tier, price=price))

    db.session.commit()
    log_activity(f"Edited product {p.name}")
    flash("Product updated.", "success")
    return redirect(url_for("admin.products"))


@admin.route("/products/<int:id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_product(id):
    p = Product.query.get_or_404(id)
    name = p.name
    db.session.delete(p)
    db.session.commit()
    log_activity(f"Deleted product {name}")
    flash("Product deleted.", "success")
    return redirect(url_for("admin.products"))


# ---------------------------------------------------------------------------
# Recipe / Bill of Materials - lets one product consume MULTIPLE inventory
# items per unit sold (e.g. 1 buffet guest = rice + chicken + drink + sauce),
# instead of the old one-product-to-one-inventory-item limitation.
# ---------------------------------------------------------------------------
@admin.route("/products/<int:id>/recipe/add", methods=["POST"])
@login_required
@admin_required
def add_recipe_item(id):
    product = Product.query.get_or_404(id)
    inventory_item_id = request.form.get("inventory_item_id")
    try:
        qty = float(request.form.get("quantity_per_unit", 0) or 0)
    except (TypeError, ValueError):
        flash("Enter a valid ingredient quantity.", "danger")
        return redirect(url_for("admin.products"))

    if not inventory_item_id:
        flash("Select an inventory item to add as an ingredient.", "danger")
        return redirect(url_for("admin.products"))
    if qty <= 0:
        flash("Ingredient quantity must be greater than zero.", "danger")
        return redirect(url_for("admin.products"))

    existing = Recipe.query.filter_by(product_id=product.id, inventory_item_id=inventory_item_id).first()
    if existing:
        existing.quantity_per_unit = qty
        flash(f"Updated ingredient for {product.name}.", "success")
    else:
        db.session.add(Recipe(product_id=product.id, inventory_item_id=inventory_item_id, quantity_per_unit=qty))
        flash(f"Ingredient added to {product.name}'s recipe.", "success")
    db.session.commit()
    log_activity(f"Updated recipe for {product.name}")
    return redirect(url_for("admin.products"))


@admin.route("/products/<int:product_id>/recipe/<int:recipe_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_recipe_item(product_id, recipe_id):
    r = Recipe.query.filter_by(id=recipe_id, product_id=product_id).first_or_404()
    product_name = r.product.name
    db.session.delete(r)
    db.session.commit()
    log_activity(f"Removed a recipe ingredient from {product_name}")
    flash("Ingredient removed.", "success")
    return redirect(url_for("admin.products"))


@admin.route("/categories/add", methods=["POST"])
@login_required
@admin_required
def add_category():
    name = request.form.get("name")
    if name and not Category.query.filter_by(name=name).first():
        db.session.add(Category(name=name))
        db.session.commit()
        flash("Category added.", "success")
    return redirect(url_for("admin.products"))


# ---------------------------------------------------------------------------
# Activity logs / login history
# ---------------------------------------------------------------------------
@admin.route("/activity-logs")
@login_required
@admin_required
def activity_logs():
    page = request.args.get("page", 1, type=int)
    pagination = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).paginate(
        page=page, per_page=100, error_out=False
    )
    return render_template("admin/activity_logs.html", logs=pagination.items, pagination=pagination)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@admin.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    if request.method == "POST":
        for key, value in request.form.items():
            setting = Setting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                db.session.add(Setting(key=key, value=value))
        db.session.commit()
        log_activity("Updated system settings")
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))

    items = {s.key: s.value for s in Setting.query.all()}
    return render_template("admin/settings.html", settings=items)


# ---------------------------------------------------------------------------
# Database backup (JSON export of key tables)
# ---------------------------------------------------------------------------
@admin.route("/backup")
@login_required
@admin_required
def backup():
    return render_template("admin/backup.html")


@admin.route("/backup/download")
@login_required
@admin_required
def backup_download():
    import io

    data = {
        "exported_at": datetime.utcnow().isoformat(),
        "sales": [
            {
                "sale_number": s.sale_number,
                "customer_name": s.customer_name,
                "total": float(s.total) if s.total is not None else 0.0,
                "status": s.status,
                "created_at": s.created_at.isoformat(),
            }
            for s in Sale.query.all()
        ],
        "reservations": [
            {
                "reservation_number": r.reservation_number,
                "customer_name": r.customer_name,
                "date": r.date.isoformat(),
                "status": r.status,
            }
            for r in Reservation.query.all()
        ],
        "expenses": [
            {"category": e.category, "amount": float(e.amount) if e.amount is not None else 0.0, "date": e.date.isoformat()}
            for e in Expense.query.all()
        ],
    }
    buf = io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))
    buf.seek(0)
    log_activity("Downloaded database backup")
    return send_file(
        buf,
        mimetype="application/json",
        as_attachment=True,
        download_name=f"sitioverde_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json",
    )
