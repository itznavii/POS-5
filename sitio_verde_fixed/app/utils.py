import io
import os
import uuid
from decimal import Decimal, InvalidOperation

from flask import current_app, request
from flask_login import current_user
from werkzeug.utils import secure_filename

from app import db
from app.models import ActivityLog


# ---------------------------------------------------------------------------
# Money helper
# ---------------------------------------------------------------------------
TWO_PLACES = Decimal("0.01")


def to_money(value, default="0"):
    """Safely convert a form/JSON value to a 2-decimal-place Decimal for
    currency math. Converting via str() (not directly from a float) avoids
    inheriting binary floating-point noise, e.g. Decimal(0.1) != Decimal("0.1").
    Raises ValueError on anything that isn't a valid number, so callers can
    return a clean 400 instead of silently coercing garbage input to 0."""
    if value is None or value == "":
        value = default
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"'{value}' is not a valid amount")
    return d.quantize(TWO_PLACES)


# ---------------------------------------------------------------------------
# Numbering helpers
# ---------------------------------------------------------------------------
def new_sale_number():
    return "SALE-" + uuid.uuid4().hex[:8].upper()


def new_reservation_number():
    return "RES-" + uuid.uuid4().hex[:6].upper()


def new_session_number():
    return "TAB-" + uuid.uuid4().hex[:6].upper()


def new_queue_number():
    """Sequential queue number that resets conceptually per day (based on count)."""
    from app.models import Sale
    from datetime import date

    today = date.today()
    count = Sale.query.filter(
        Sale.is_walkin.is_(True), db.func.date(Sale.created_at) == today
    ).count()
    return f"Q-{count + 1:03d}"


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
def log_activity(action):
    try:
        user_id = current_user.id if current_user.is_authenticated else None
        entry = ActivityLog(
            user_id=user_id,
            action=action,
            ip_address=request.remote_addr if request else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


# ---------------------------------------------------------------------------
# Secure file upload
# ---------------------------------------------------------------------------
def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_UPLOAD_EXTENSIONS"]


def save_upload(file_storage, subfolder=""):
    """Safely persist an uploaded file, returns the stored relative filename."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        raise ValueError("File type not allowed")
    safe_name = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex[:10]}_{safe_name}"
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], subfolder)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, unique_name)
    file_storage.save(dest)
    return os.path.join(subfolder, unique_name) if subfolder else unique_name


# ---------------------------------------------------------------------------
# Inventory deduction (Recipe / BOM aware)
# ---------------------------------------------------------------------------
def get_required_inventory(sale_item, product):
    """{inventory_item_id: qty_needed} for one sale item, using the
    product's Recipe (or legacy single-item link) - see
    Product.ingredient_requirements()."""
    reqs = {}
    if not product:
        return reqs
    for inv_item, qty_per_unit in product.ingredient_requirements():
        if not inv_item or not qty_per_unit:
            continue
        reqs[inv_item.id] = reqs.get(inv_item.id, 0) + qty_per_unit * sale_item.quantity
    return reqs


def check_inventory_availability(items_with_products):
    """items_with_products: [(sale_item, product), ...] not yet committed.
    Returns a list of shortfall dicts (empty = everything is available), so
    a sale can be rejected BEFORE it's created instead of silently letting
    stock go negative."""
    from app.models import InventoryItem

    needed_totals = {}
    for sale_item, product in items_with_products:
        for inv_id, qty in get_required_inventory(sale_item, product).items():
            needed_totals[inv_id] = needed_totals.get(inv_id, 0) + qty

    shortfalls = []
    for inv_id, needed in needed_totals.items():
        item = InventoryItem.query.get(inv_id)
        if not item:
            continue
        available = item.quantity or 0
        if needed > available + 1e-9:
            shortfalls.append({"item": item, "needed": needed, "available": available})
    return shortfalls


def deduct_inventory_for_sale_item(sale_item, product):
    from app.models import InventoryTransaction

    if not product:
        return
    for inv_item, qty_per_unit in product.ingredient_requirements():
        if not inv_item or not qty_per_unit:
            continue
        qty_to_deduct = qty_per_unit * sale_item.quantity
        new_qty = (inv_item.quantity or 0) - qty_to_deduct
        if new_qty < 0:
            # Should not happen if check_inventory_availability() was called
            # first - this is just a defensive floor against a race with a
            # concurrent sale, logged so it doesn't go unnoticed.
            current_app.logger.warning(
                f"Inventory for '{inv_item.name}' would go negative deducting for "
                f"sale item {sale_item.id} ({product.name}); flooring at 0 instead."
            )
            new_qty = 0
        inv_item.quantity = new_qty
        db.session.add(
            InventoryTransaction(
                item_id=inv_item.id,
                type="sale_deduction",
                quantity=qty_to_deduct,
                reference=str(sale_item.sale_id),
                note=f"Sold: {product.name}",
                user_id=current_user.id if current_user.is_authenticated else None,
            )
        )


def check_low_stock_and_notify():
    from app.models import InventoryItem
    from app.notifications.helpers import notify

    low_items = InventoryItem.query.filter(
        InventoryItem.quantity <= InventoryItem.low_stock_threshold
    ).all()
    for item in low_items:
        notify(
            "low_inventory",
            f"Low stock: {item.name} ({item.quantity} {item.unit} left)",
            related_id=item.id,
            # Stable per-item key so repeated checks update one alert rather
            # than creating a new row every time a sale touches this item.
            dedupe_key=f"low_stock:{item.id}",
        )


# ---------------------------------------------------------------------------
# Receipt PDF (thermal 80mm) with QR code
# ---------------------------------------------------------------------------
def generate_receipt_pdf(sale, restaurant_name="Sitio Verde Buffet Restaurant"):
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    import qrcode

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=(80 * mm, 180 * mm))
    width = 80 * mm

    y = 170 * mm
    p.setFont("Helvetica-Bold", 11)
    p.drawCentredString(width / 2, y, restaurant_name)
    y -= 5 * mm
    p.setFont("Helvetica", 8)
    # NOTE: intentionally NOT "Official Receipt" - this system is not an
    # accredited BIR point-of-sale, so it must not represent this document
    # as a legally-compliant official receipt.
    p.drawCentredString(width / 2, y, "SALES RECEIPT (Customer Copy)")
    y -= 4 * mm
    p.line(5 * mm, y, 75 * mm, y)
    y -= 5 * mm

    p.setFont("Helvetica", 8)
    p.drawString(5 * mm, y, f"Receipt #: {sale.sale_number}")
    y -= 4 * mm
    if sale.reservation_id:
        p.drawString(5 * mm, y, f"Reservation #: {sale.reservation.reservation_number}")
        y -= 4 * mm
    p.drawString(5 * mm, y, f"Date: {sale.created_at.strftime('%Y-%m-%d %H:%M')}")
    y -= 4 * mm
    cashier_name = sale.cashier.name if sale.cashier else "N/A"
    p.drawString(5 * mm, y, f"Cashier: {cashier_name}")
    y -= 4 * mm
    p.drawString(5 * mm, y, f"Customer: {sale.customer_name or 'Walk-in'}")
    y -= 4 * mm
    if sale.table:
        p.drawString(5 * mm, y, f"Table: {sale.table.name}")
        y -= 4 * mm
    if sale.queue_number:
        p.drawString(5 * mm, y, f"Queue #: {sale.queue_number}")
        y -= 4 * mm
    y -= 2 * mm
    p.line(5 * mm, y, 75 * mm, y)
    y -= 5 * mm

    p.setFont("Helvetica-Bold", 8)
    p.drawString(5 * mm, y, "Item")
    p.drawString(50 * mm, y, "Qty")
    p.drawString(60 * mm, y, "Amount")
    y -= 4 * mm
    p.setFont("Helvetica", 8)
    for item in sale.items:
        label = item.product_name
        if item.is_buffet:
            label += " (Buffet)"
        p.drawString(5 * mm, y, label[:28])
        p.drawString(50 * mm, y, str(item.quantity))
        p.drawString(60 * mm, y, f"{item.line_total:.2f}")
        y -= 4 * mm
        if item.is_buffet:
            breakdown = []
            if item.buffet_adult:
                breakdown.append(f"Adult x{item.buffet_adult}")
            if item.buffet_senior:
                breakdown.append(f"Senior x{item.buffet_senior}")
            if item.buffet_pwd:
                breakdown.append(f"PWD x{item.buffet_pwd}")
            if item.buffet_kids:
                breakdown.append(f"Kids x{item.buffet_kids}")
            if item.buffet_free:
                breakdown.append(f"Free x{item.buffet_free}")
            if breakdown:
                p.setFont("Helvetica-Oblique", 7)
                p.drawString(6 * mm, y, ", ".join(breakdown)[:40])
                y -= 4 * mm
                p.setFont("Helvetica", 8)

    y -= 2 * mm
    p.line(5 * mm, y, 75 * mm, y)
    y -= 5 * mm

    p.setFont("Helvetica", 8)
    p.drawString(5 * mm, y, f"Subtotal: PHP {sale.subtotal:.2f}")
    y -= 4 * mm
    if sale.discount:
        p.drawString(5 * mm, y, f"Discount ({sale.discount_type}): -PHP {sale.discount:.2f}")
        y -= 4 * mm
    if sale.senior_pwd_savings:
        p.drawString(5 * mm, y, f"Senior/PWD price savings: PHP {sale.senior_pwd_savings:.2f}")
        y -= 4 * mm
    if sale.reservation_id and sale.reservation:
        applied_dp = sale.reservation.total_verified_paid()
        if applied_dp:
            p.drawString(5 * mm, y, f"Less Down Payment: -PHP {applied_dp:.2f}")
            y -= 4 * mm
    p.setFont("Helvetica-Bold", 9)
    p.drawString(5 * mm, y, f"TOTAL: PHP {sale.total:.2f}")
    y -= 5 * mm
    p.setFont("Helvetica", 8)

    for pay in sale.payments:
        ref = f" ({pay.reference_number})" if pay.reference_number else ""
        p.drawString(5 * mm, y, f"{pay.method}: PHP {pay.amount:.2f}{ref}")
        y -= 4 * mm

    if sale.refunds:
        for rf in sale.refunds:
            label = "VOID - Refunded" if rf.refund_type == "void" else "Refund"
            p.drawString(5 * mm, y, f"{label}: -PHP {rf.amount:.2f}")
            y -= 4 * mm
        p.setFont("Helvetica-Bold", 8)
        p.drawString(5 * mm, y, f"Net paid: PHP {sale.net_paid():.2f}")
        y -= 4 * mm
        p.setFont("Helvetica", 8)

    if sale.amount_tendered:
        p.drawString(5 * mm, y, f"Tendered: PHP {sale.amount_tendered:.2f}")
        y -= 4 * mm
        p.drawString(5 * mm, y, f"Change: PHP {sale.change:.2f}")
        y -= 4 * mm

    y -= 3 * mm
    # QR code linking to the sale number for verification
    try:
        qr = qrcode.QRCode(box_size=2, border=1)
        qr.add_data(f"SITIOVERDE|{sale.sale_number}|{sale.total:.2f}")
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_buf = io.BytesIO()
        qr_img.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        from reportlab.lib.utils import ImageReader

        qr_size = 22 * mm
        p.drawImage(
            ImageReader(qr_buf),
            (width - qr_size) / 2,
            y - qr_size,
            width=qr_size,
            height=qr_size,
        )
        y -= qr_size + 3 * mm
    except Exception:
        pass

    p.setFont("Helvetica-Oblique", 8)
    p.drawCentredString(width / 2, y, "Thank you for dining with us!")

    p.showPage()
    p.save()
    buf.seek(0)
    return buf
