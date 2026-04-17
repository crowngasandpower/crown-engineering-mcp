"""LDAP authentication and session management."""

import os
import secrets
from datetime import datetime, timedelta, timezone

import ldap3
from sqlalchemy.orm import Session as DBSession

from models import Session, User

LDAP_SERVER = os.environ.get("LDAP_SERVER", "")  # Must be set via env var
LDAP_PORT = int(os.environ.get("LDAP_PORT", "389"))
LDAP_DOMAIN = os.environ.get("LDAP_DOMAIN", "crowngp.local")
LDAP_BASE_DN = os.environ.get("LDAP_BASE_DN", "DC=crowngp,DC=local")
ENGINEER_GROUP_DN = os.environ.get(
    "ENGINEER_GROUP_DN",
    "CN=Dev-Env-Access,OU=Security Groups,DC=crowngp,DC=local",
)
INITIAL_ADMIN = os.environ.get("INITIAL_ADMIN", "paul.berry-briggs")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "8"))


def authenticate_user(username: str, password: str) -> dict | None:
    """Bind to LDAP as the user and return their profile, or None on failure.

    Returns dict with keys: display_name, email, is_engineer.
    """
    upn = f"{username}@{LDAP_DOMAIN}"
    server = ldap3.Server(LDAP_SERVER, port=LDAP_PORT)
    try:
        conn = ldap3.Connection(server, user=upn, password=password, auto_bind=True)
    except ldap3.core.exceptions.LDAPBindError:
        return None
    except ldap3.core.exceptions.LDAPSocketOpenError:
        return None

    conn.search(
        LDAP_BASE_DN,
        f"(sAMAccountName={ldap3.utils.conv.escape_filter_chars(username)})",
        attributes=["displayName", "mail", "memberOf"],
    )

    if not conn.entries:
        conn.unbind()
        return None

    entry = conn.entries[0]
    member_of = [str(g) for g in entry.memberOf] if entry.memberOf else []
    is_engineer = ENGINEER_GROUP_DN in member_of

    result = {
        "display_name": str(entry.displayName) if entry.displayName else username,
        "email": str(entry.mail) if entry.mail else "",
        "is_engineer": is_engineer,
    }

    conn.unbind()
    return result


def upsert_user(db: DBSession, username: str, ldap_info: dict) -> User:
    """Create or update a user record from LDAP info. Returns the User."""
    now = datetime.now(timezone.utc).isoformat()
    user = db.query(User).filter(User.username == username).first()

    if user:
        user.display_name = ldap_info["display_name"]
        user.email = ldap_info["email"]
        user.is_engineer = int(ldap_info["is_engineer"])
        user.last_login = now
    else:
        user = User(
            username=username,
            display_name=ldap_info["display_name"],
            email=ldap_info["email"],
            is_engineer=int(ldap_info["is_engineer"]),
            is_admin=1 if username == INITIAL_ADMIN else 0,
            first_login=now,
            last_login=now,
        )
        db.add(user)

    db.commit()
    db.refresh(user)
    return user


def create_session(db: DBSession, user_id: int, ip: str) -> str:
    """Create a new session and return the token."""
    now = datetime.now(timezone.utc)
    token = secrets.token_hex(32)

    session = Session(
        id=token,
        user_id=user_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=SESSION_HOURS)).isoformat(),
        ip_address=ip,
    )
    db.add(session)
    db.commit()
    return token


def validate_session(db: DBSession, token: str) -> User | None:
    """Look up a session token and return the associated User, or None."""
    if not token:
        return None

    session = db.query(Session).filter(Session.id == token).first()
    if not session:
        return None

    now = datetime.now(timezone.utc).isoformat()
    if session.expires_at < now:
        db.delete(session)
        db.commit()
        return None

    user = db.query(User).filter(User.id == session.user_id).first()
    return user


def delete_session(db: DBSession, token: str) -> None:
    """Delete a session by token."""
    db.query(Session).filter(Session.id == token).delete()
    db.commit()


def purge_expired_sessions(db: DBSession) -> int:
    """Delete all expired sessions. Returns count deleted."""
    now = datetime.now(timezone.utc).isoformat()
    count = db.query(Session).filter(Session.expires_at < now).delete()
    db.commit()
    return count
