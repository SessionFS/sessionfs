"""Tests for the global IntegrityError handler — v0.10.24 tk_e7da4c4508d94bac.

Closes GH #51 ask #2 (najitestech): bare 'Internal Server Error' on
SQLAlchemy IntegrityError leaks past route handlers and gives clients
no actionable detail.

Two test layers:

1. Unit tests for ``_classify_integrity_error`` — feed synthetic
   IntegrityError objects with mocked ``orig.pgcode`` (PostgreSQL) and
   with raw SQLite message text. Verifies the (code, message, status)
   tuple for each violation class.
2. Integration tests via the FastAPI TestClient — register a tiny
   route that raises a real IntegrityError, hit it, assert envelope
   shape + status code. Proves the handler is wired correctly through
   ``register_exception_handlers``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from sessionfs.server.errors import (
    _classify_integrity_error,
    register_exception_handlers,
)


class _FakeOrig:
    """Minimal DBAPI-error shape: carries pgcode + str() returning the
    driver message. Used to feed _classify_integrity_error without
    spinning up a real connection."""

    def __init__(self, pgcode: str | None, message: str) -> None:
        self.pgcode = pgcode
        self._msg = message

    def __str__(self) -> str:
        return self._msg


def _make_integrity_error(pgcode: str | None, message: str) -> IntegrityError:
    """Construct an IntegrityError that mimics asyncpg/aiosqlite shape.

    SQLAlchemy IntegrityError(statement, params, orig). `orig.pgcode` is
    the PostgreSQL SQLSTATE; SQLite drivers don't carry it.
    """
    return IntegrityError(
        statement="SELECT 1", params={}, orig=_FakeOrig(pgcode, message)
    )


# -- Unit-level classification -----------------------------------------------


class TestClassifyIntegrityError:
    def test_pg_unique_violation_409(self):
        exc = _make_integrity_error("23505", "duplicate key value violates unique constraint \"x\"")
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "duplicate_resource"
        assert status == 409

    def test_pg_fk_violation_500(self):
        exc = _make_integrity_error("23503", 'insert or update on table "org_members" violates foreign key')
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "foreign_key_violation"
        # FK violations are server bugs in our model, not user input
        # errors — surface as 500 so the diagnosis isn't masked as 4xx.
        assert status == 500

    def test_pg_not_null_violation_422(self):
        exc = _make_integrity_error("23502", 'null value in column "title" violates not-null constraint')
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "missing_required_field"
        assert status == 422

    def test_pg_check_violation_422(self):
        exc = _make_integrity_error("23514", 'new row for relation "x" violates check constraint "x_chk"')
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "check_constraint_violation"
        assert status == 422

    def test_sqlite_unique_violation_409(self):
        # SQLite drivers don't expose pgcode — fall through to string match.
        exc = _make_integrity_error(None, "UNIQUE constraint failed: users.email")
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "duplicate_resource"
        assert status == 409

    def test_sqlite_fk_violation_500(self):
        exc = _make_integrity_error(None, "FOREIGN KEY constraint failed")
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "foreign_key_violation"
        assert status == 500

    def test_sqlite_not_null_violation_422(self):
        exc = _make_integrity_error(None, "NOT NULL constraint failed: tickets.title")
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "missing_required_field"
        assert status == 422

    def test_unknown_integrity_error_falls_through_to_500(self):
        # Unrecognised pgcode + unrecognised SQLite message — still
        # surface a structured envelope (not bare 'Internal Server
        # Error'), still 500.
        exc = _make_integrity_error("99999", "something exotic went wrong")
        code, _, _, status = _classify_integrity_error(exc)
        assert code == "integrity_error"
        assert status == 500


# -- Integration: full envelope shape via TestClient -------------------------


def _build_test_app(violation_text: str, pgcode: str | None) -> FastAPI:
    """Spin up a tiny FastAPI app with our exception handlers attached
    and one route that raises an IntegrityError. Lets us exercise the
    full registered-handler path without dragging in the real engine."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/_test/integrity")
    def raise_integrity() -> dict:
        raise _make_integrity_error(pgcode, violation_text)

    return app


class TestIntegrityErrorHandler:
    def test_unique_violation_envelope(self):
        app = _build_test_app("UNIQUE constraint failed: users.email", None)
        with TestClient(app) as client:
            resp = client.get("/_test/integrity")
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "duplicate_resource"
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]
        # Raw DBAPI text must NOT leak through — strips column names etc.
        assert "users.email" not in body["error"]["message"]

    def test_fk_violation_envelope_500(self):
        app = _build_test_app("FOREIGN KEY constraint failed", None)
        with TestClient(app) as client:
            resp = client.get("/_test/integrity")
        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["error"]["code"] == "foreign_key_violation"
        # Critical: the 500 body MUST have structured content. Bare
        # "Internal Server Error" was the entire reason for this ticket.
        assert body["error"]["message"] != "Internal Server Error"

    def test_unknown_integrity_error_still_envelope(self):
        app = _build_test_app("something exotic", "99999")
        with TestClient(app) as client:
            resp = client.get("/_test/integrity")
        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["error"]["code"] == "integrity_error"
        # Even the unknown class returns a structured body, not plain
        # text — that's the contract the CLI/dashboard parser depends on.
        assert "error" in body and isinstance(body["error"], dict)
