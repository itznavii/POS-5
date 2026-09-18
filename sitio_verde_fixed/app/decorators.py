from functools import wraps

from flask import abort
from flask_login import current_user


def role_required(*roles):
    """Restrict a view to the given roles, e.g. @role_required('admin')."""

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(403)
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(f):
    return role_required("admin")(f)
