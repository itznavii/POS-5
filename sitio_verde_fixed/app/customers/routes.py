from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Customer, Sale, Reservation
from app.utils import log_activity

customers = Blueprint("customers", __name__)


@customers.route("/")
@login_required
def list_customers():
    q = (request.args.get("q") or "").strip()
    query = Customer.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Customer.name.ilike(like), Customer.phone.ilike(like)))
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Customer.name).paginate(page=page, per_page=25, error_out=False)
    return render_template("customers/list.html", items=pagination.items, pagination=pagination, q=q)


@customers.route("/add", methods=["POST"])
@login_required
def add_customer():
    name = request.form.get("name")
    if name:
        c = Customer(
            name=name,
            phone=request.form.get("phone"),
            email=request.form.get("email"),
            preferred_events=request.form.get("preferred_events"),
            notes=request.form.get("notes"),
        )
        db.session.add(c)
        db.session.commit()
        log_activity(f"Added customer {name}")
        flash("Customer added.", "success")
    return redirect(url_for("customers.list_customers"))


@customers.route("/<int:id>")
@login_required
def detail(id):
    c = Customer.query.get_or_404(id)
    sales = Sale.query.filter_by(customer_id=id).order_by(Sale.created_at.desc()).all()
    reservation_history = Reservation.query.filter_by(customer_id=id).order_by(Reservation.date.desc()).all()
    return render_template("customers/detail.html", c=c, sales=sales, reservation_history=reservation_history)
