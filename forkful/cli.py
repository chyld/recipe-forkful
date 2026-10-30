"""Command-line user management: the only way accounts are created (there is no sign-up page)."""

import sqlite3
import sys

import click
from flask import current_app
from flask.cli import with_appcontext

from . import auth
from .db import MigrationError, connect, migrate, transaction, utc_ts


def fail(message):
    click.echo(f"Error: {message}", err=True)
    sys.exit(1)


def read_password(password_stdin):
    if password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < auth.MIN_PASSWORD:
        fail(f"Passwords need at least {auth.MIN_PASSWORD} characters.")
    if len(password) > auth.MAX_PASSWORD:
        fail(f"Passwords can't be longer than {auth.MAX_PASSWORD} characters.")
    return password


def open_db():
    db_path = current_app.config["DATABASE"]
    if not db_path.exists():
        fail(f"No database at {db_path}. Run `flask migrate` first.")
    return connect(db_path)


@click.command("migrate")
@with_appcontext
def migrate_command():
    """Create or upgrade the database schema."""
    try:
        old, applied = migrate(current_app.config["DATABASE"])
    except MigrationError as exc:
        fail(str(exc))
    if applied:
        click.echo(f"Database migrated from version {old} to {applied[-1]}.")
    else:
        click.echo(f"Database is up to date (version {old}).")


@click.command("create-user")
@with_appcontext
@click.argument("username")
@click.option("--password-stdin", is_flag=True, help="Read the password from one line of stdin.")
def create_user_command(username, password_stdin):
    """Create a user who can log in."""
    if not auth.valid_username(username):
        fail("Usernames are 1-32 characters: letters, digits, dot, dash, underscore.")
    password = read_password(password_stdin)
    db = open_db()
    try:
        with transaction(db):
            db.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, auth.hash_password(password), utc_ts()),
            )
    except sqlite3.IntegrityError:
        fail(f"A user named {username!r} already exists (usernames ignore case).")
    finally:
        db.close()
    click.echo(f"Created user {username!r}. Happy cooking!")


@click.command("set-password")
@with_appcontext
@click.argument("username")
@click.option("--password-stdin", is_flag=True, help="Read the password from one line of stdin.")
def set_password_command(username, password_stdin):
    """Change a user's password and log them out everywhere."""
    if not auth.valid_username(username):
        fail(f"No user named {username!r}.")
    db = open_db()
    try:
        if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is None:
            fail(f"No user named {username!r}.")
        password = read_password(password_stdin)
        with transaction(db):
            row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                fail(f"No user named {username!r}.")
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (auth.hash_password(password), row["id"]))
            ended = db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],)).rowcount
    finally:
        db.close()
    click.echo(f"Password changed for {username!r}; ended {ended} session(s).")


def init_app(app):
    app.cli.add_command(migrate_command)
    app.cli.add_command(create_user_command)
    app.cli.add_command(set_password_command)
