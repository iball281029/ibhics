"""Users, roles, sessions and the audit log of the manager's web console and API.

Passwords are hashed with scrypt from the standard library. Sessions are random
tokens kept in memory: a manager restart signs everyone out, which is acceptable for
an admin console and keeps secrets off disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
MIN_PASSWORD = 8
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class Role(StrEnum):
    VIEWER = "viewer"  # sees everything, changes nothing
    OPERATOR = "operator"  # runs VMs: power, console, create, delete, back up, restore, migrate
    ADMIN = "admin"  # also nodes, storage, backup targets and schedules, users

    @property
    def rank(self) -> int:
        return list(Role).index(self)


class AuthError(Exception):
    """Not signed in, or the session expired (HTTP 401)."""


class Forbidden(Exception):
    """Signed in, but the role does not allow it (HTTP 403)."""


class UserNotFound(LookupError):
    pass


@dataclass
class User:
    name: str
    role: Role
    password_hash: str
    enabled: bool = True
    must_change_password: bool = False
    created: str = ""
    last_login: str | None = None


@dataclass
class Session:
    token: str
    user: str
    created: float
    last_used: float


@dataclass
class AuditEntry:
    time: str
    user: str
    action: str
    target: str
    result: int
    address: str


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = hash_password(password, bytes.fromhex(salt)).split("$")[2]
    return hmac.compare_digest(expected, digest)


def check_password_rules(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if password.lower() == username.lower():
        raise ValueError("password must differ from the username")


# A hash to compare against when the user does not exist, so a wrong username takes
# as long as a wrong password and does not reveal which accounts exist.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


class Auth:
    def __init__(
        self,
        state_path: Path | None = None,
        audit_path: Path | None = None,
        idle_timeout: float = 30 * 60,
        max_age: float = 12 * 3600,
        max_failures: int = 5,
        lockout: float = 5 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_path = state_path
        self._audit_path = audit_path
        self.idle_timeout = idle_timeout
        self._max_age = max_age
        self._max_failures = max_failures
        self._lockout = lockout
        self._clock = clock
        self._users: dict[str, User] = {}
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._audit: deque[AuditEntry] = deque(maxlen=1000)
        self._lock = threading.RLock()

    # Persistence

    def load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text())
        with self._lock:
            for entry in data["users"]:
                entry["role"] = Role(entry["role"])
                self._users[entry["name"]] = User(**entry)

    def _save(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        users = [{**asdict(u), "role": str(u.role)} for u in self._users.values()]
        # Password hashes only, but still nobody else's business.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"users": users}, f, indent=2)
        tmp.replace(self._state_path)

    def bootstrap(self, password: str | None = None) -> str | None:
        """Create the first admin if there are no users. Returns the generated password
        when one was made up, so the caller can show it once."""
        with self._lock:
            if self._users:
                return None
            generated = None
            if not password:
                password = generated = secrets.token_urlsafe(12)
            self._users["admin"] = User(
                name="admin",
                role=Role.ADMIN,
                password_hash=hash_password(password),
                must_change_password=True,
                created=self._stamp(),
            )
            self._save()
            return generated

    # Users

    def users(self) -> list[User]:
        with self._lock:
            return list(self._users.values())

    def user(self, name: str) -> User:
        with self._lock:
            try:
                return self._users[name]
            except KeyError:
                raise UserNotFound(name) from None

    def add_user(self, name: str, role: Role, password: str) -> User:
        if not USERNAME_RE.match(name):
            raise ValueError("username: 2-32 lowercase letters, digits, '_', '.', '-'")
        check_password_rules(password, name)
        with self._lock:
            if name in self._users:
                raise ValueError(f"user {name!r} already exists")
            user = User(
                name=name,
                role=role,
                password_hash=hash_password(password),
                must_change_password=True,  # the admin chose it; the user should replace it
                created=self._stamp(),
            )
            self._users[name] = user
            self._save()
            return user

    def update_user(
        self,
        name: str,
        role: Role | None = None,
        enabled: bool | None = None,
        password: str | None = None,
        acting: str | None = None,
    ) -> User:
        with self._lock:
            user = self.user(name)
            demoting = role is not None and role != Role.ADMIN
            if user.role == Role.ADMIN and (demoting or enabled is False):
                self._keep_an_admin(name)
            if name == acting and (demoting or enabled is False):
                raise ValueError("you cannot demote or disable your own account")
            if role is not None:
                user.role = role
            if enabled is not None:
                user.enabled = enabled
            if password is not None:
                check_password_rules(password, name)
                user.password_hash = hash_password(password)
                user.must_change_password = True
            if enabled is False or password is not None or role is not None:
                self._end_sessions(name)
            self._save()
            return user

    def delete_user(self, name: str, acting: str | None = None) -> None:
        with self._lock:
            user = self.user(name)
            if name == acting:
                raise ValueError("you cannot delete your own account")
            if user.role == Role.ADMIN:
                self._keep_an_admin(name)
            del self._users[name]
            self._end_sessions(name)
            self._save()

    def change_password(self, name: str, old: str, new: str) -> None:
        with self._lock:
            user = self.user(name)
            if not verify_password(old, user.password_hash):
                raise Forbidden("current password is wrong")
            if old == new:
                raise ValueError("choose a password different from the current one")
            check_password_rules(new, name)
            user.password_hash = hash_password(new)
            user.must_change_password = False
            self._save()

    def _keep_an_admin(self, name: str) -> None:
        others = [
            u for u in self._users.values() if u.role == Role.ADMIN and u.enabled and u.name != name
        ]
        if not others:
            raise ValueError("the cluster needs at least one enabled admin")

    # Sessions

    def login(self, name: str, password: str) -> Session:
        now = self._clock()
        with self._lock:
            recent = [t for t in self._failures.get(name, []) if now - t < self._lockout]
            self._failures[name] = recent
            if len(recent) >= self._max_failures:
                raise AuthError("too many failed sign-ins; try again in a few minutes")
            user = self._users.get(name)
            ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
            if not (ok and user and user.enabled):
                recent.append(now)
                raise AuthError("wrong username or password")
            self._failures.pop(name, None)
            user.last_login = self._stamp()
            session = Session(secrets.token_urlsafe(32), name, now, now)
            self._sessions[session.token] = session
            self._save()
            return session

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def authenticate(self, token: str | None) -> User:
        if not token:
            raise AuthError("sign in first")
        now = self._clock()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise AuthError("sign in first")
            expired = (
                now - session.last_used > self.idle_timeout or now - session.created > self._max_age
            )
            user = self._users.get(session.user)
            if expired or user is None or not user.enabled:
                del self._sessions[token]
                raise AuthError("your session has ended; sign in again")
            session.last_used = now
            return user

    def _end_sessions(self, name: str) -> None:
        for token in [t for t, s in self._sessions.items() if s.user == name]:
            del self._sessions[token]

    # Audit log

    def record(self, user: str, action: str, target: str, result: int, address: str) -> None:
        entry = AuditEntry(self._stamp(), user, action, target, result, address)
        with self._lock:
            self._audit.append(entry)
            if self._audit_path:
                self._audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self._audit_path.open("a") as f:
                    f.write(json.dumps(asdict(entry)) + "\n")

    def audit(self, limit: int = 200) -> list[AuditEntry]:
        with self._lock:
            return list(reversed(self._audit))[:limit]

    def _stamp(self) -> str:
        return datetime.fromtimestamp(self._clock()).astimezone().isoformat(timespec="seconds")
