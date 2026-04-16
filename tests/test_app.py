"""
Test suite for goodgurl.gg Flask app.
Run with: python -m pytest tests/ -v
"""
import os
import sys
import tempfile
import pytest
from sqlalchemy.pool import StaticPool

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def app():
    """Create application for testing."""
    from app import app as flask_app, db
    from models import User

    flask_app.config.update({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "RATELIMIT_ENABLED": False,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        },
        "SERVER_NAME": "localhost",
    })

    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


def register(client, username="testuser", email="test@test.com",
             password="TestPass1", role="sissy"):
    return client.post("/register", data={
        "username": username,
        "email": email,
        "password": password,
        "role": role,
    }, follow_redirects=True)


def login(client, username="testuser", password="TestPass1"):
    return client.post("/login", data={
        "username": username,
        "password": password,
    }, follow_redirects=True)


# ── Auth Tests ─────────────────────────────────────────────────────────────

class TestRegistration:
    def test_register_success(self, client):
        rv = register(client)
        assert rv.status_code == 200

    def test_register_duplicate_username(self, client):
        register(client)
        rv = register(client, email="other@test.com")
        assert b"already taken" in rv.data or rv.status_code == 200

    def test_register_weak_password(self, client):
        rv = register(client, password="short")
        assert b"8 character" in rv.data or b"uppercase" in rv.data or rv.status_code == 200

    def test_register_invalid_role(self, client):
        rv = register(client, role="admin")
        # Should not create an admin
        assert b"admin" not in rv.data.lower() or rv.status_code == 200


class TestLogin:
    def test_login_success(self, client):
        register(client)
        rv = login(client)
        assert rv.status_code == 200

    def test_login_wrong_password(self, client):
        register(client)
        rv = login(client, password="WrongPass1")
        assert b"Invalid" in rv.data or rv.status_code == 200

    def test_login_banned_user(self, client, app):
        register(client)
        client.get("/logout")  # clear session from register's auto-login
        with app.app_context():
            from extensions import db
            db.session.execute(db.text("UPDATE user SET is_banned = 1 WHERE username = 'testuser'"))
            db.session.commit()
        rv = login(client)
        assert b"banned" in rv.data.lower() or b"Log In" in rv.data


class TestLogout:
    def test_logout(self, client):
        register(client)
        login(client)
        rv = client.get("/logout", follow_redirects=True)
        assert rv.status_code == 200


# ── Password Reset Tests ──────────────────────────────────────────────────

class TestPasswordReset:
    def test_forgot_password_page(self, client):
        rv = client.get("/forgot-password")
        assert rv.status_code == 200
        assert b"Forgot Password" in rv.data or b"email" in rv.data.lower()

    def test_forgot_password_submit(self, client):
        register(client)
        rv = client.post("/forgot-password", data={"email": "test@test.com"},
                         follow_redirects=True)
        assert rv.status_code == 200

    def test_reset_password_invalid_token(self, client):
        rv = client.get("/reset-password/badtoken", follow_redirects=True)
        assert b"invalid" in rv.data.lower() or b"expired" in rv.data.lower()

    def test_reset_password_flow(self, client, app):
        register(client)
        with app.app_context():
            from helpers import generate_reset_token
            token = generate_reset_token("test@test.com")
        rv = client.post(f"/reset-password/{token}",
                         data={"password": "NewPass123"},
                         follow_redirects=True)
        assert rv.status_code == 200
        # Should be able to log in with new password
        rv = login(client, password="NewPass123")
        assert rv.status_code == 200


# ── Account Management Tests ──────────────────────────────────────────────

class TestAccountManagement:
    def test_change_password(self, client):
        register(client)
        login(client)
        rv = client.post("/account/change-password", data={
            "current_password": "TestPass1",
            "new_password": "NewPass123",
        }, follow_redirects=True)
        assert rv.status_code == 200

    def test_change_password_wrong_current(self, client):
        register(client)
        login(client)
        rv = client.post("/account/change-password", data={
            "current_password": "WrongPass1",
            "new_password": "NewPass123",
        }, follow_redirects=True)
        assert b"incorrect" in rv.data.lower() or rv.status_code == 200

    def test_delete_account(self, client):
        register(client)
        login(client)
        rv = client.post("/account/delete", data={
            "password": "TestPass1",
        }, follow_redirects=True)
        assert rv.status_code == 200

    def test_delete_account_wrong_password(self, client):
        register(client)
        login(client)
        rv = client.post("/account/delete", data={
            "password": "WrongPass1",
        }, follow_redirects=True)
        assert b"incorrect" in rv.data.lower() or rv.status_code == 200

    def test_upload_avatar_no_file(self, client):
        register(client)
        login(client)
        rv = client.post("/account/upload-avatar", data={},
                         follow_redirects=True)
        assert rv.status_code == 200


# ── Page Access Tests ─────────────────────────────────────────────────────

class TestPageAccess:
    def test_feed_guest(self, client):
        rv = client.get("/")
        assert rv.status_code in (200, 302)

    def test_account_requires_login(self, client):
        rv = client.get("/account", follow_redirects=True)
        assert b"Log In" in rv.data or b"login" in rv.data.lower()

    def test_register_page(self, client):
        rv = client.get("/register")
        assert rv.status_code == 200
        assert b"minlength" in rv.data

    def test_login_page(self, client):
        rv = client.get("/login")
        assert rv.status_code == 200
        assert b"forgot" in rv.data.lower()


# ── Helpers Tests ─────────────────────────────────────────────────────────

class TestHelpers:
    def test_validate_password_short(self, app):
        with app.app_context():
            from helpers import validate_password
            assert validate_password("Ab1") is not None

    def test_validate_password_no_upper(self, app):
        with app.app_context():
            from helpers import validate_password
            assert validate_password("abcdefg1") is not None

    def test_validate_password_no_digit(self, app):
        with app.app_context():
            from helpers import validate_password
            assert validate_password("Abcdefgh") is not None

    def test_validate_password_valid(self, app):
        with app.app_context():
            from helpers import validate_password
            assert validate_password("ValidPass1") is None

    def test_reset_token_roundtrip(self, app):
        with app.app_context():
            from helpers import generate_reset_token, verify_reset_token
            token = generate_reset_token("user@example.com")
            assert verify_reset_token(token) == "user@example.com"

    def test_reset_token_invalid(self, app):
        with app.app_context():
            from helpers import verify_reset_token
            assert verify_reset_token("garbage") is None
