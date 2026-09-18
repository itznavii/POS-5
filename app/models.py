from datetime import datetime
from decimal import Decimal

from flask_login import UserMixin

from app import db

# All money columns use this instead of raw db.Numeric so the precision/scale
# is consistent everywhere and easy to change in one place. Numeric (not
# Float) avoids binary floating-point rounding errors in financial figures
# (e.g. 0.1 + 0.2 != 0.3 in IEEE 754 float, but is exact in Decimal).
def Money(default=0):
    return db.Column(db.Numeric(12, 2), default=default)


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default="staff", nullable=False)  # admin | staff
    name = db.Column(db.String(120))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Forces a password change on next login (used for auto-generated
    # first-run credentials and admin-issued password resets).
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)

    # Login brute-force protection
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    def is_admin(self):
        return self.role == "admin"


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    preferred_events = db.Column(db.String(200))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sales = db.relationship("Sale", backref="customer", lazy=True)
    reservations = db.relationship("Reservation", backref="customer", lazy=True)


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    products = db.relationship("Product", backref="category", lazy=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    selling_price = Money()
    cost_price = Money()
    description = db.Column(db.Text)
    image = db.Column(db.String(255))
    barcode = db.Column(db.String(64), unique=True, nullable=True, index=True)
    available = db.Column(db.Boolean, default=True)
    is_buffet = db.Column(db.Boolean, default=False)

    # DEPRECATED single-item inventory link. Still honored as a fallback for
    # products that don't have any Recipe rows, so existing data keeps
    # working, but new products should use `recipe_items` instead - a real
    # restaurant product usually consumes several ingredients per unit sold
    # (e.g. one buffet guest = rice + chicken + drink + sauce), not just one.
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_item.id"))
    deduct_qty = db.Column(db.Float, default=1.0)

    buffet_tiers = db.relationship(
        "BuffetTier", backref="product", lazy=True, cascade="all, delete-orphan"
    )
    recipe_items = db.relationship(
        "Recipe", backref="product", lazy=True, cascade="all, delete-orphan"
    )

    def tier_price(self, tier):
        for t in self.buffet_tiers:
            if t.tier == tier:
                return t.price
        return Decimal("0.00")

    def ingredient_requirements(self):
        """Returns [(InventoryItem, qty_needed_per_unit_sold), ...] - the
        product's Recipe/BOM if one is defined, otherwise falls back to the
        legacy single inventory_item_id/deduct_qty link. Empty list means
        this product doesn't track inventory at all."""
        if self.recipe_items:
            return [(r.inventory_item, r.quantity_per_unit) for r in self.recipe_items if r.inventory_item]
        if self.inventory_item_id and self.inventory_item:
            return [(self.inventory_item, self.deduct_qty or 0)]
        return []


class BuffetTier(db.Model):
    """Per-product buffet pricing tiers: adult / senior / pwd / kids / free."""

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    tier = db.Column(db.String(20), nullable=False)  # adult, senior, pwd, kids, free
    price = Money()


class Recipe(db.Model):
    """One ingredient line in a product's Bill of Materials: how much of a
    given inventory item is consumed each time 1 unit of the product is
    sold (1 guest, for buffet products). A product can have many of these,
    which replaces the old "one product -> one inventory item" limitation."""

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_item.id"), nullable=False)
    quantity_per_unit = db.Column(db.Float, nullable=False, default=0)

    inventory_item = db.relationship("InventoryItem")


class DiningSession(db.Model):
    """A party's visit to one or more tables, from seating to close-out.
    Replaces the old model where a table just held a status string and a
    single Sale - a real dine-in visit can span multiple orders (rounds of
    food), a split bill, and multiple tables pushed together (a merge).

    TABLE(s) -> DiningSession -> Sale(s) -> SaleItem(s) -> kitchen -> payment -> closed
    """

    id = db.Column(db.Integer, primary_key=True)
    session_number = db.Column(db.String(20), unique=True, nullable=False)
    status = db.Column(db.String(20), default="open")  # open | closed

    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    customer_name = db.Column(db.String(120))
    pax = db.Column(db.Integer, default=1)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"))

    opened_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    opened_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime)

    tables = db.relationship("RestaurantTable", backref="session", lazy=True)
    sales = db.relationship("Sale", backref="dining_session", lazy=True)
    opened_by = db.relationship("User", foreign_keys=[opened_by_id])

    def active_sales(self):
        return [s for s in self.sales if s.status != "voided"]

    def total_billed(self):
        return sum((s.total for s in self.active_sales()), Decimal("0.00"))

    def total_paid(self):
        return sum((s.net_paid() for s in self.active_sales()), Decimal("0.00"))

    def balance_due(self):
        return max(Decimal("0.00"), self.total_billed() - self.total_paid())

    def table_names(self):
        return ", ".join(t.name for t in self.tables)


class RestaurantTable(db.Model):
    __tablename__ = "restaurant_table"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), nullable=False)  # e.g. "T1"
    capacity = db.Column(db.Integer, default=4)
    status = db.Column(db.String(20), default="available")
    # available | occupied | reserved | cleaning
    merged_into_id = db.Column(db.Integer, db.ForeignKey("restaurant_table.id"))
    session_id = db.Column(db.Integer, db.ForeignKey("dining_session.id"))

    sales = db.relationship("Sale", backref="table", lazy=True)


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_number = db.Column(db.String(20), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    customer_name = db.Column(db.String(120))
    is_walkin = db.Column(db.Boolean, default=False)
    queue_number = db.Column(db.String(10))
    table_id = db.Column(db.Integer, db.ForeignKey("restaurant_table.id"))
    dining_session_id = db.Column(db.Integer, db.ForeignKey("dining_session.id"))
    # A "tab" order is sent to the kitchen and deducts inventory immediately,
    # but doesn't require full payment up front - the balance is settled
    # when the whole DiningSession is closed (supports multiple rounds of
    # food and a bill split across several payments).
    is_tab = db.Column(db.Boolean, default=False)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"))

    subtotal = Money()
    # Actual monetary reduction applied to the bill (promo/manual discounts
    # only). Senior/PWD tiered pricing is NOT added in here - see
    # senior_pwd_savings below - since it was never charged in the first
    # place, so counting it again as a "discount" double-counted it.
    discount = Money()
    discount_type = db.Column(db.String(30), default="none")
    # Informational only: how much less senior/PWD guests paid vs the adult
    # tier price, for reporting/receipt display. Does not reduce `total`
    # again - it's already baked into `subtotal` via the senior/PWD tier price.
    senior_pwd_savings = Money()

    vat = Money()  # not currently calculated (kept at 0) - no VAT feature
    total = Money()

    amount_tendered = Money()
    change = Money()

    status = db.Column(db.String(20), default="completed")
    # open (kitchen in-progress) | completed | voided
    void_reason = db.Column(db.String(255))
    voided_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    requires_approval = db.Column(db.Boolean, default=False)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    cashier_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship(
        "SaleItem", backref="sale", lazy=True, cascade="all, delete-orphan"
    )
    payments = db.relationship(
        "SalePayment", backref="sale", lazy=True, cascade="all, delete-orphan"
    )
    refunds = db.relationship(
        "SaleRefund", backref="sale", lazy=True, cascade="all, delete-orphan"
    )
    attachments = db.relationship(
        "SaleAttachment", backref="sale", lazy=True, cascade="all, delete-orphan"
    )
    cashier = db.relationship("User", foreign_keys=[cashier_id])
    reservation = db.relationship("Reservation", foreign_keys=[reservation_id])

    def total_paid(self):
        return sum((p.amount for p in self.payments), Decimal("0.00"))

    def total_refunded(self):
        return sum((r.amount for r in self.refunds), Decimal("0.00"))

    def net_paid(self):
        """What the customer has actually paid, net of refunds. This is the
        figure payment reports should use, not total_paid() alone."""
        return self.total_paid() - self.total_refunded()

    def refundable_amount(self):
        """How much of this sale could still be refunded."""
        return max(Decimal("0.00"), self.total_paid() - self.total_refunded())


class SaleRefund(db.Model):
    """Financial reversal ledger for a sale: full voids and partial refunds
    both record an entry here instead of just deleting/ignoring the original
    SalePayment rows, so payment reports stay accurate."""

    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sale.id"), nullable=False)
    amount = Money()
    reason = db.Column(db.String(255))
    refund_type = db.Column(db.String(20), default="partial")  # partial | void
    method = db.Column(db.String(30))  # how the money was returned
    refunded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    refunded_by = db.relationship("User", foreign_keys=[refunded_by_id])


class SaleAttachment(db.Model):
    """Proof-of-payment file (screenshot/receipt) attached to a sale."""

    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sale.id"))
    filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sale.id"))
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    product_name = db.Column(db.String(120))
    quantity = db.Column(db.Integer, default=1)
    price = Money()
    line_total = Money()

    is_senior_pwd = db.Column(db.Boolean, default=False)  # per-line discount flag

    is_buffet = db.Column(db.Boolean, default=False)
    buffet_adult = db.Column(db.Integer, default=0)
    buffet_senior = db.Column(db.Integer, default=0)
    buffet_pwd = db.Column(db.Integer, default=0)
    buffet_kids = db.Column(db.Integer, default=0)
    buffet_free = db.Column(db.Integer, default=0)

    kitchen_status = db.Column(db.String(20), default="preparing")
    # preparing | ready | served | completed


class SalePayment(db.Model):
    """Supports split / mixed payments per sale."""

    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sale.id"))
    method = db.Column(db.String(30))  # Cash, GCash, Bank Transfer, Credit Card
    amount = Money()
    reference_number = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reservation_number = db.Column(db.String(20), unique=True, nullable=False)
    inquiry_number = db.Column(db.String(20))
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))

    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.Time, nullable=False)
    status = db.Column(db.String(20), default="Reserved")
    # Reserved, Confirmed, Checked In, Completed, Cancelled, No Show

    customer_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    pax = db.Column(db.Integer, nullable=False)
    event_type = db.Column(db.String(50))
    special_requests = db.Column(db.Text)
    assigned_staff = db.Column(db.String(120))
    confirmed_by = db.Column(db.String(120))
    date_confirmed = db.Column(db.DateTime)

    # DEPRECATED as a source of financial truth - kept only as a
    # display/legacy cache of the amount recorded at booking time. Anything
    # that needs to know how much has actually been paid must use
    # total_verified_paid() / total_recorded_paid() below, which read the
    # ReservationPayment ledger instead.
    down_payment = Money()

    arrival_time = db.Column(db.DateTime)
    actual_pax = db.Column(db.Integer)
    total_bill = Money()
    remaining_balance = Money()
    # Set when a staff member (non-admin) enters a final total that looks
    # suspiciously low vs. the standard per-head rate; an admin must approve
    # it before the reservation can be marked Completed.
    billing_requires_approval = db.Column(db.Boolean, default=False)
    billing_approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attachments = db.relationship(
        "ReservationAttachment",
        backref="reservation",
        lazy=True,
        cascade="all, delete-orphan",
    )
    res_payments = db.relationship(
        "ReservationPayment",
        backref="reservation",
        lazy=True,
        cascade="all, delete-orphan",
    )


    def total_verified_paid(self):
        """The financial source of truth for how much has been paid and
        confirmed - reads the ReservationPayment ledger, not the legacy
        down_payment field."""
        return sum(
            (p.amount for p in self.res_payments if p.status == "Verified"),
            Decimal("0.00"),
        )

    def total_recorded_paid(self):
        """Includes payments still awaiting verification, for staff-facing
        views where that distinction matters (e.g. "pending" badge)."""
        return sum(
            (p.amount for p in self.res_payments if p.status != "Rejected"),
            Decimal("0.00"),
        )


class ReservationAttachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"))
    filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class ReservationPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"))
    payment_type = db.Column(db.String(20))  # down_payment | final_payment
    method = db.Column(db.String(30))
    amount = Money()
    reference_number = db.Column(db.String(60))
    status = db.Column(db.String(20), default="Pending")  # Pending, Verified, Rejected
    paid_at = db.Column(db.DateTime, default=datetime.utcnow)
    verified_by = db.Column(db.String(120))


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    contact = db.Column(db.String(120))

    items = db.relationship("InventoryItem", backref="supplier", lazy=True)


class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    quantity = db.Column(db.Float, default=0)
    unit = db.Column(db.String(20), default="pcs")
    low_stock_threshold = db.Column(db.Float, default=5)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))

    products = db.relationship("Product", backref="inventory_item", lazy=True)
    transactions = db.relationship(
        "InventoryTransaction", backref="item", lazy=True, cascade="all, delete-orphan"
    )


class InventoryTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("inventory_item.id"))
    # in | out | adjustment | sale_deduction | void_reversal | waste | spoilage
    type = db.Column(db.String(20))
    quantity = db.Column(db.Float, default=0)
    reference = db.Column(db.String(120))
    note = db.Column(db.String(255))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50))
    description = db.Column(db.String(200))
    amount = Money()
    date = db.Column(db.Date, nullable=False)
    recorded_by = db.Column(db.Integer, db.ForeignKey("user.id"))


class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True)
    value = db.Column(db.String(255))


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(255))
    ip_address = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])


class CashierShift(db.Model):
    """A cashier's till session: opening float, everything taken in during
    the shift, and the counted cash at close-out. Gives the restaurant a
    real end-of-day reconciliation (expected vs actual, with a variance)
    instead of just a pile of sales rows."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    opening_cash = Money()
    closing_cash_counted = Money()  # what the cashier physically counted
    status = db.Column(db.String(20), default="open")  # open | closed
    opened_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime)
    notes = db.Column(db.String(255))
    approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    user = db.relationship("User", foreign_keys=[user_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    def _window_end(self):
        return self.closed_at or datetime.utcnow()

    def sales_in_shift(self):
        return Sale.query.filter(
            Sale.cashier_id == self.user_id,
            Sale.created_at >= self.opened_at,
            Sale.created_at <= self._window_end(),
            Sale.status == "completed",
        ).all()

    def payments_by_method(self):
        """{method: total} across this shift's completed sales."""
        totals = {}
        for s in self.sales_in_shift():
            for p in s.payments:
                key = (p.method or "Unknown").title()
                totals[key] = totals.get(key, Decimal("0.00")) + (p.amount or Decimal("0.00"))
        return totals

    def cash_sales(self):
        return self.payments_by_method().get("Cash", Decimal("0.00"))

    def total_refunds(self):
        total = Decimal("0.00")
        for s in self.sales_in_shift():
            total += s.total_refunded()
        return total

    def expected_cash(self):
        """Opening float + cash taken in - cash refunded out."""
        return (self.opening_cash or Decimal("0.00")) + self.cash_sales() - self.total_refunds()

    def variance(self):
        """Counted minus expected. Negative = till is short."""
        if self.closing_cash_counted is None:
            return None
        return self.closing_cash_counted - self.expected_cash()


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(40))
    # reservation_reminder | low_inventory | cancelled_reservation | discount_approval | new_reservation
    message = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, default=False)
    related_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Who this notification belongs to. NULL = a broadcast notice visible to
    # everyone. Previously there was no owner at all, so one staff member
    # marking something read cleared it for the whole restaurant.
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    # Stable key for deduplication, e.g. "low_stock:17" - lets repeat alerts
    # for the same underlying condition collapse into one row.
    dedupe_key = db.Column(db.String(120), index=True)

    user = db.relationship("User", foreign_keys=[user_id])
