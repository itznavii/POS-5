# Sitio Verde Buffet Restaurant — POS & Reservation Management System

A full-stack Flask POS and reservation system built to be an actual working
cashier system, not just a checklist demo. Built around the spec you provided,
with the gaps that make the difference between "checklist POS" and a real one
filled in:

- **Split / mixed payments** per sale (multiple payment rows: Cash + GCash + …)
- **Correct discount math** — Senior/PWD 20% applies per flagged line item
  (not the whole receipt), buffet items use their own tiered pricing
  (adult/senior/PWD/kids/free), promo % stacks on top, VAT computed after
  discount
- **Large-discount approval workflow** — a staff cashier applying a big
  discount creates a held sale that pings admins for approval before it
  posts and deducts inventory
- **Inventory that actually deducts on sale** (simple per-product stock
  deduction, as you asked), with stock in/out/adjustment history, low-stock
  notifications, and automatic restock when a sale is voided
- **Table management** with status tiles, transfer, and merge
- **Kitchen order queue** synced live to each sale's line items
  (preparing → ready → served → completed)
- **Full reservation lifecycle**: create → confirm → check-in (actual pax) →
  final billing (auto-computes remaining balance against *verified* down
  payments) → official receipt → marked Completed
- **Void/refund** (admin-only), which reverses the inventory deduction
- **Reports** with real CSV, Excel (.xlsx), and PDF export
- **Role-based access exactly per your spec** — staff/cashier cannot delete
  products or reservations, view financial reports, manage users, or access
  settings/backup
- Thermal-style receipt PDFs with QR code, activity/login logs, notifications,
  customer database, expense tracking, JSON backup export

## ⚠️ Important — this was not run end-to-end before delivery

The sandbox this was built in has no network access, so I could not
`pip install` Flask-SQLAlchemy / Flask-Login / Flask-Migrate / Flask-WTF /
qrcode, etc., or actually launch the server to smoke-test the full app.

What **was** verified without a live server:
- Every `.py` file passes `python -m py_compile` (no syntax errors)
- Every `url_for(...)` referenced in every template was cross-checked
  against actual Flask route function names — all match
- Every SQLAlchemy relationship/backref accessed in templates and routes
  was cross-checked against `models.py` — two real bugs were found this way
  and fixed (a reference-field type mismatch on void/restock, and a missing
  `Sale.reservation` relationship the receipt PDF depends on)

What was **not** verified: actual runtime behavior. There is a real chance
a first run surfaces something a live server would have caught immediately
(a typo, an edge case in a query, a template variable name). If that
happens, send me the traceback and I'll fix it right away.

## Setup (local)

```bash
python3.12 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # edit SECRET_KEY, DATABASE_URL as needed
# For quick local testing you can skip DATABASE_URL — it falls back to a
# local SQLite file. NEVER use SQLite in production, per the original spec.

python seed.py                    # creates tables + admin/staff + sample menu
python run.py                     # http://localhost:5000
```

Demo logins after seeding:
- **Admin:** `admin` / `admin123`
- **Staff/Cashier:** `staff` / `staff123`

## Deploying to Render with PostgreSQL

1. Push this project to GitHub.
2. On Render: create a **PostgreSQL** instance, copy its internal
   `DATABASE_URL`.
3. Create a **Web Service** from the repo:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn run:app`
4. Set environment variables: `SECRET_KEY`, `DATABASE_URL`,
   `UPLOAD_FOLDER=app/static/uploads`.
5. After first deploy, run once (Render Shell or a one-off job):
   ```bash
   python seed.py
   ```
6. For schema changes going forward, use Flask-Migrate:
   ```bash
   flask db init      # once
   flask db migrate -m "message"
   flask db upgrade
   ```

## Project structure

```
app/
  auth/          login/logout
  main/          dashboard + chart APIs
  pos/           checkout, barcode lookup, receipts, void, approvals
  tables/        table layout, status, transfer, merge
  kitchen/       kitchen order queue
  reservations/  full reservation lifecycle incl. final billing
  inventory/     stock in/out/adjust, suppliers, low-stock
  reports/       sales/reservation/payment/expense reports + export
  expenses/      expense tracking
  customers/     customer database
  admin/         staff accounts, menu management, logs, settings, backup
  notifications/ in-app notification center
  models.py      full schema
  utils.py       receipt PDF, numbering, inventory deduction, uploads
  decorators.py  role_required / admin_required
seed.py          creates demo accounts + sample menu/tables/inventory
run.py           entrypoint
```

## Known simplifications (be aware of these before going live)

- **Backup** is a JSON export of key business data for record-keeping, not
  a binary Postgres dump — use Render's managed Postgres backups for real
  disaster recovery.
- **VAT/Senior-PWD compliance** here is a reasonable approximation (20% off
  the flagged line, VAT computed after discount) — it is *not* a substitute
  for a BIR-compliant POS accreditation, which has additional requirements
  (e.g. official VAT-exempt sales invoices for senior/PWD) beyond this
  project's scope.
- Barcode support is a text-input "scan" (works with any USB/Bluetooth
  barcode scanner that types + Enter, which is how virtually all of them
  behave) rather than a camera-based scanner.
