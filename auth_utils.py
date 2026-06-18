"""Authentication decorators for TrainerPro.

These guard routes by checking the Flask session. They intentionally have no
dependency on app.py, so they can be imported anywhere without circular imports.
"""
from functools import wraps
from flask import session, redirect, url_for


def login_required(f):
    """Require a logged-in trainer (user_id in session)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def client_login_required(f):
    """Require a logged-in client (client_account_id in session)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'client_account_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function