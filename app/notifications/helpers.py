from datetime import datetime, timedelta

from app import db
from app.models import Notification


def notify(ntype, message, related_id=None, dedupe=False, dedupe_key=None, user_id=None):
    """Create a notification.

    user_id: who it belongs to. None = a broadcast notice everyone sees.
    dedupe_key: a stable identifier for the underlying condition (e.g.
    "low_stock:17"). When supplied, an existing UNREAD notification with the
    same key is reused instead of piling up duplicate rows for the same
    problem. Falls back to the older time-window/message matching when no
    key is given.
    """
    if dedupe_key:
        existing = Notification.query.filter_by(
            dedupe_key=dedupe_key, is_read=False
        ).first()
        if existing:
            # Refresh the message/timestamp so the alert reflects current numbers.
            existing.message = message
            existing.created_at = datetime.utcnow()
            db.session.commit()
            return existing
    elif dedupe:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        existing = (
            Notification.query.filter_by(type=ntype, message=message, is_read=False)
            .filter(Notification.created_at >= cutoff)
            .first()
        )
        if existing:
            return existing

    n = Notification(
        type=ntype,
        message=message,
        related_id=related_id,
        dedupe_key=dedupe_key,
        user_id=user_id,
    )
    db.session.add(n)
    db.session.commit()
    return n


def _visible_to(user):
    """Notifications addressed to this user plus broadcast ones."""
    return Notification.query.filter(
        db.or_(Notification.user_id == user.id, Notification.user_id.is_(None))
    )


def unread_notification_count(user=None):
    from flask_login import current_user

    user = user or current_user
    if not user or not user.is_authenticated:
        return 0
    return _visible_to(user).filter(Notification.is_read.is_(False)).count()
