from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required

from app import db
from app.models import SaleItem, Sale
from app.utils import log_activity

kitchen = Blueprint("kitchen", __name__)


@kitchen.route("/")
@login_required
def queue():
    items = (
        SaleItem.query.join(Sale)
        .filter(Sale.status.in_(["completed", "open"]), SaleItem.kitchen_status != "completed")
        .order_by(Sale.created_at)
        .all()
    )
    return render_template("kitchen/queue.html", items=items)


@kitchen.route("/api/queue")
@login_required
def api_queue():
    items = (
        SaleItem.query.join(Sale)
        .filter(Sale.status.in_(["completed", "open"]), SaleItem.kitchen_status != "completed")
        .order_by(Sale.created_at)
        .all()
    )
    return jsonify(
        [
            {
                "id": i.id,
                "sale_number": i.sale.sale_number,
                "table": i.sale.table.name if i.sale.table else None,
                "product_name": i.product_name,
                "quantity": i.quantity,
                "status": i.kitchen_status,
            }
            for i in items
        ]
    )


@kitchen.route("/<int:id>/status", methods=["POST"])
@login_required
def update_status(id):
    item = SaleItem.query.get_or_404(id)
    status = request.form.get("status") or (request.get_json(silent=True) or {}).get("status")
    if status in ("preparing", "ready", "served", "completed"):
        item.kitchen_status = status
        db.session.commit()
        log_activity(f"Kitchen item {item.product_name} -> {status}")
    if request.is_json:
        return jsonify({"ok": True})
    from flask import redirect, url_for
    return redirect(url_for("kitchen.queue"))
