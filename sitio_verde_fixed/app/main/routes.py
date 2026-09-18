import os
from datetime import date, datetime, time, timedelta

from flask import Blueprint, render_template, jsonify, current_app, send_from_directory, abort
from flask_login import login_required, current_user
from sqlalchemy import func

from app import db
from app.models import (
    Sale,
    SaleItem,
    Reservation,
    InventoryItem,
    RestaurantTable,
)

main = Blueprint("main", __name__)


@main.route("/files/uploads/<path:filename>")
@login_required
def serve_upload(filename):
    """Serve uploaded files (payment proofs, product images, etc.) from
    outside the public static folder, gated behind login. These files can
    contain names, phone numbers, bank/GCash reference numbers and QR
    codes, so they must never be directly web-accessible."""
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    full_path = os.path.normpath(os.path.join(upload_folder, filename))
    # Guard against path traversal (e.g. "../../etc/passwd").
    if not full_path.startswith(os.path.normpath(upload_folder) + os.sep):
        abort(404)
    directory, name = os.path.split(full_path)
    if not os.path.isfile(full_path):
        abort(404)
    return send_from_directory(directory, name)


@main.route("/")
@login_required
def dashboard():
    today = date.today()
    tomorrow = today + timedelta(days=1)

    # Use a datetime range rather than func.date(...) so the query can use an
    # index on created_at - func.date() forces a full scan as data grows.
    today_start = datetime.combine(today, time.min)
    tomorrow_start = datetime.combine(tomorrow, time.min)

    today_sales = (
        db.session.query(func.sum(Sale.total))
        .filter(
            Sale.created_at >= today_start,
            Sale.created_at < tomorrow_start,
            Sale.status == "completed",
        )
        .scalar()
        or 0
    )
    today_reservations = Reservation.query.filter_by(date=today).count()
    # Renamed from "walkin_now": an occupied table isn't necessarily a
    # walk-in (it could be a reservation, event, or transferred table).
    occupied_tables = RestaurantTable.query.filter_by(status="occupied").count()
    upcoming = Reservation.query.filter(
        Reservation.date > today, Reservation.status.in_(["Reserved", "Confirmed"])
    ).count()
    month_start = datetime.combine(today.replace(day=1), time.min)
    monthly = (
        db.session.query(func.sum(Sale.total))
        .filter(
            Sale.created_at >= month_start,
            Sale.created_at < tomorrow_start,
            Sale.status == "completed",
        )
        .scalar()
        or 0
    )
    low_stock = InventoryItem.query.filter(
        InventoryItem.quantity <= InventoryItem.low_stock_threshold
    ).count()
    recent_sales = Sale.query.order_by(Sale.created_at.desc()).limit(8).all()

    # Scoped to the current month and to non-voided sales - previously this
    # scanned the entire sales history, so the "best seller" shown on a
    # dashboard of today's numbers could reflect a product from years ago.
    best_seller_row = (
        db.session.query(SaleItem.product_name, func.sum(SaleItem.quantity).label("qty"))
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(
            Sale.created_at >= month_start,
            Sale.created_at < tomorrow_start,
            Sale.status == "completed",
        )
        .group_by(SaleItem.product_name)
        .order_by(func.sum(SaleItem.quantity).desc())
        .first()
    )
    best_seller = best_seller_row[0] if best_seller_row else "N/A"

    upcoming_list = (
        Reservation.query.filter(
            Reservation.date >= today, Reservation.status.in_(["Reserved", "Confirmed"])
        )
        .order_by(Reservation.date, Reservation.time)
        .limit(6)
        .all()
    )

    return render_template(
        "dashboard.html",
        today_sales=today_sales,
        today_reservations=today_reservations,
        occupied_tables=occupied_tables,
        upcoming=upcoming,
        monthly=monthly,
        low_stock=low_stock,
        recent_sales=recent_sales,
        best_seller=best_seller,
        upcoming_list=upcoming_list,
    )


@main.route("/api/sales-chart")
@login_required
def sales_chart():
    days = []
    totals = []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        total = (
            db.session.query(func.sum(Sale.total))
            .filter(func.date(Sale.created_at) == d, Sale.status == "completed")
            .scalar()
            or 0
        )
        days.append(d.strftime("%a"))
        totals.append(round(float(total), 2))
    return jsonify({"labels": days, "data": totals})


@main.route("/api/reservation-chart")
@login_required
def reservation_chart():
    statuses = ["Reserved", "Confirmed", "Checked In", "Completed", "Cancelled", "No Show"]
    counts = [Reservation.query.filter_by(status=s).count() for s in statuses]
    return jsonify({"labels": statuses, "data": counts})
