"""Manual seed script — safe to run repeatedly (only fills in missing data).
On Render this now also runs automatically on every app startup, so you
normally don't need to run this by hand. It's kept for local development.
"""
from app import create_app
from app.seed_data import run_seed

app = create_app()

with app.app_context():
    run_seed()
    print("Seed complete. Login with admin/admin123 or staff/staff123")
