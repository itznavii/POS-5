from flask import Blueprint, render_template, redirect, url_for, request, abort
from flask_login import login_required, current_user

from app import db
from app.models import Notification
from app.notifications.helpers import _visible_to

notifications = Blueprint("notifications", __name__)


@notifications.route("/")
@login_required
def list_notifications():
    page = request.args.get("page", 1, type=int)
    pagination = (
        _visible_to(current_user)
        .order_by(Notification.created_at.desc())
        .paginate(page=page, per_page=50, error_out=False)
    )
    return render_template(
        "notifications/list.html", items=pagination.items, pagination=pagination
    )


@notifications.route("/<int:id>/read")
@login_required
def mark_read(id):
    n = Notification.query.get_or_404(id)
    # Only the owner (or anyone, for broadcast notices) may mark it read -
    # otherwise one user could clear another user's notifications.
    if n.user_id is not None and n.user_id != current_user.id:
        abort(403)
    n.is_read = True
    db.session.commit()
    return redirect(url_for("notifications.list_notifications"))


@notifications.route("/read-all")
@login_required
def mark_all_read():
    # Scoped to what this user can actually see, rather than a blanket
    # update that silently marked every staff member's alerts as read.
    _visible_to(current_user).filter(Notification.is_read.is_(False)).update(
        {Notification.is_read: True}, synchronize_session=False
    )
    db.session.commit()
    return redirect(url_for("notifications.list_notifications"))
