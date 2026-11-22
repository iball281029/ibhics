"""Users, roles, sessions and the audit log of the manager's web console and API.

Passwords use scrypt from the standard library. Persisted sessions contain only
hashes of random bearer tokens. TOTP secrets and hashed recovery codes are kept in
the private user state file and never included in API user records.
"""

from __future__ import annotations

import base64
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
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from .events import publish
from .permissions import BUILTIN, CATEGORIES
from .totp import verify_totp

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
    role: Role | str
    password_hash: str
    enabled: bool = True
    must_change_password: bool = False
    created: str = ""
    last_login: str | None = None
    totp_secret: str = field(default="", repr=False)
    totp_pending: str = field(default="", repr=False)
    totp_pending_until: float = 0
    totp_last_step: int = -1
    recovery_hashes: list[str] = field(default_factory=list, repr=False)
    require_totp: bool = False  # an admin asked this user to set up 2FA
    permissions: list[str] = field(default_factory=list)


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


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
        # Keyed by a hash of the token: the saved copy cannot be replayed as a cookie.
        self._sessions: dict[str, Session] = {}
        self._sessions_path = state_path.with_name("sessions.json") if state_path else None
        self._sessions_saved = 0.0
        self._failures: dict[str, list[float]] = {}
        self._audit: deque[AuditEntry] = deque(maxlen=1000)
        self._lock = threading.RLock()
        self.custom_roles: dict[str, list[str]] = {}
        self.require_2fa = False

    # Persistence

    def load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text())
        with self._lock:
            self.custom_roles = data.get("roles", {})
            self.require_2fa = data.get("require_2fa", False)
            security = {}
            sidecar = self._state_path.with_name("users-security.json")
            if sidecar.exists():
                security = json.loads(sidecar.read_text())
                self.custom_roles = security.get("roles", self.custom_roles)
                self.require_2fa = security.get("require_2fa", self.require_2fa)
            allowed = {f.name for f in fields(User)}
            for original in data["users"]:
                entry = {k: v for k, v in original.items() if k in allowed}
                extra = security.get("users", {}).get(entry["name"], {})
                # Old code may change a base role; never restore stale elevated privileges.
                if extra.get("base_role") == entry["role"]:
                    entry.update({k: v for k, v in extra.items() if k in allowed})
                if entry["role"] in BUILTIN:
                    entry["role"] = Role(entry["role"])
                self._users[entry["name"]] = User(**entry)
            self._sync_permissions()
            self._load_sessions()

    def _load_sessions(self) -> None:
        """Sessions survive a manager restart (an upgrade), so nobody is signed out."""
        path = self._sessions_path
        if not path or not path.exists():
            return
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        now = self._clock()
        for key, s in saved.items():
            session = Session("", s["user"], float(s["created"]), float(s["last_used"]))
            if s["user"] in self._users and not self._expired(session, now):
                self._sessions[key] = session

    def _save_sessions(self, force: bool = True) -> None:
        path = self._sessions_path
        now = self._clock()
        if not path or (not force and now - self._sessions_saved < 60):
            return
        self._sessions_saved = now
        data = {
            k: {"user": s.user, "created": s.created, "last_used": s.last_used}
            for k, s in self._sessions.items()
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        tmp.replace(path)

    def _save(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        from .settings import private_json

        legacy = {
            "name",
            "role",
            "password_hash",
            "enabled",
            "must_change_password",
            "created",
            "last_login",
        }
        users, extra = [], {}
        for user in self._users.values():
            record = {**asdict(user), "role": str(user.role)}
            base = {k: v for k, v in record.items() if k in legacy}
            base["role"] = str(user.role) if str(user.role) in BUILTIN else "viewer"
            users.append(base)
            extra[user.name] = {k: v for k, v in record.items() if k not in legacy}
            extra[user.name].update(role=str(user.role), base_role=base["role"])
        private_json(
            self._state_path.with_name("users-security.json"),
            {
                "users": extra,
                "roles": self.custom_roles,
                "require_2fa": self.require_2fa,
            },
        )
        private_json(self._state_path, {"users": users})

    def bootstrap(self, password: str | None = None, must_change: bool = True) -> str | None:
        """Create the first admin if there are no users. Returns the generated password
        when one was made up, so the caller can show it once. `must_change=False` is for
        a password the admin chose themselves at the node's console."""
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
                must_change_password=must_change or generated is not None,
                created=self._stamp(),
            )
            self._sync_permissions()
            self._save()
            return generated

    # Roles are permissions to manage a category. Every role may view inventory.

    def _sync_permissions(self):
        for user in self._users.values():
            user.permissions = sorted(
                BUILTIN.get(str(user.role), self.custom_roles.get(str(user.role), []))
            )

    def permissions(self, user: User) -> set[str]:
        return set(BUILTIN.get(str(user.role), self.custom_roles.get(str(user.role), [])))

    def define_role(self, name: str, permissions: list[str]):
        if not USERNAME_RE.fullmatch(name) or name in BUILTIN:
            raise ValueError("use a unique lowercase custom role name (2-32 characters)")
        if not set(permissions) <= set(CATEGORIES):
            raise ValueError("unknown permission category")
        with self._lock:
            self.custom_roles[name] = sorted(set(permissions))
            self._sync_permissions()
            for user in self._users.values():
                if user.role == name:
                    self._end_sessions(user.name)
            self._save()

    def validate_role(self, role):
        if role not in BUILTIN and role not in self.custom_roles:
            raise ValueError("unknown role")

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

    def add_user(self, name: str, role: Role | str, password: str) -> User:
        if not USERNAME_RE.match(name):
            raise ValueError("username: 2-32 lowercase letters, digits, '_', '.', '-'")
        check_password_rules(password, name)
        with self._lock:
            self.validate_role(role)
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
            self._sync_permissions()
            self._save()
            return user

    def update_user(
        self,
        name: str,
        role: Role | str | None = None,
        enabled: bool | None = None,
        password: str | None = None,
        acting: str | None = None,
        require_totp: bool | None = None,
    ) -> User:
        with self._lock:
            user = self.user(name)
            demoting = role is not None and role != Role.ADMIN
            if user.role == Role.ADMIN and (demoting or enabled is False):
                self._keep_an_admin(name)
            if name == acting and (demoting or enabled is False):
                raise ValueError("you cannot demote or disable your own account")
            # Validate the whole edit before changing any in-memory privileges.
            if role is not None:
                self.validate_role(role)
            if password is not None:
                check_password_rules(password, name)
            if role is not None:
                user.role = role
                self._sync_permissions()
            if enabled is not None:
                user.enabled = enabled
            if require_totp is not None:
                user.require_totp = require_totp
                if not require_totp:
                    self.reset_2fa(name)  # "Turn off 2FA": no code needed from now on
            if password is not None:
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

    def login(self, name: str, password: str, code: str = "") -> Session:
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
                if len(recent) == self._max_failures:
                    publish(
                        self,
                        "auth.failed_logins",
                        "Repeated failed sign-ins reached lockout threshold",
                        f"auth:{name}",
                    )
                self._forget_old_failures(now)
                raise AuthError("wrong username or password")
            if user.totp_secret and not self._second_factor(user, code):
                recent.append(now)
                if len(recent) == self._max_failures:
                    publish(
                        self,
                        "auth.failed_logins",
                        "Repeated failed sign-ins reached lockout threshold",
                        f"auth:{name}",
                    )
                raise AuthError("Enter a valid 6-digit authentication code or unused recovery code")
            self._failures.pop(name, None)
            user.last_login = self._stamp()
            self._drop_expired_sessions(now)
            session = Session(secrets.token_urlsafe(32), name, now, now)
            self._sessions[_key(session.token)] = session
            self._save()
            self._save_sessions()
            return session

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_key(token or ""), None)
            self._save_sessions()

    def authenticate(self, token: str | None) -> User:
        if not token:
            raise AuthError("sign in first")
        now = self._clock()
        with self._lock:
            key = _key(token)
            session = self._sessions.get(key)
            if session is None:
                raise AuthError("sign in first")
            expired = self._expired(session, now)
            user = self._users.get(session.user)
            if expired or user is None or not user.enabled:
                del self._sessions[key]
                self._save_sessions()
                raise AuthError("your session has ended; sign in again")
            session.last_used = now
            self._save_sessions(force=False)  # idle time survives a restart, roughly
            return user

    def _expired(self, session: Session, now: float) -> bool:
        return now - session.last_used > self.idle_timeout or now - session.created > self._max_age

    def _drop_expired_sessions(self, now: float) -> None:
        for token in [t for t, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[token]

    def _forget_old_failures(self, now: float) -> None:
        # Guesses at made-up usernames must not grow this without bound.
        if len(self._failures) > 1000:
            for name in [
                n for n, ts in self._failures.items() if not ts or now - ts[-1] >= self._lockout
            ]:
                del self._failures[name]

    def _end_sessions(self, name: str) -> None:
        for token in [t for t, s in self._sessions.items() if s.user == name]:
            del self._sessions[token]
        self._save_sessions()

    # Two-factor authentication. No secret is included in UserOut or audit records.

    def _reauth(self, user: User, password: str):
        now = self._clock()
        recent = [t for t in self._failures.get(user.name, []) if now - t < self._lockout]
        self._failures[user.name] = recent
        if len(recent) >= self._max_failures:
            raise Forbidden("too many failed attempts; try again in a few minutes")
        if not verify_password(password, user.password_hash):
            recent.append(now)
            raise Forbidden("current password is wrong")
        return recent

    def _second_factor(self, user: User, code: str) -> bool:
        step = verify_totp(user.totp_secret, code, self._clock(), user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            return True
        digest = _key(code.upper().strip())
        for stored in user.recovery_hashes:
            if hmac.compare_digest(stored, digest):
                user.recovery_hashes.remove(stored)
                return True
        return False

    def setup_2fa(self, name: str, password: str) -> dict:
        with self._lock:
            user = self.user(name)
            self._reauth(user, password)
            if user.totp_secret:
                raise ValueError("2FA is already enabled")
            user.totp_pending = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
            user.totp_pending_until = self._clock() + 600
            self._save()
            uri = (
                f"otpauth://totp/{quote('ibHiCS:' + name, safe='')}?"
                f"secret={user.totp_pending}&issuer=ibHiCS&algorithm=SHA1&digits=6&period=30"
            )
            return {"secret": user.totp_pending, "uri": uri}

    def confirm_2fa(self, name: str, code: str) -> list[str]:
        with self._lock:
            user = self.user(name)
            recent = [t for t in self._failures.get(name, []) if self._clock() - t < self._lockout]
            self._failures[name] = recent
            if len(recent) >= self._max_failures:
                raise Forbidden("too many failed attempts; try again in a few minutes")
            step = (
                verify_totp(user.totp_pending, code, self._clock())
                if user.totp_pending and self._clock() <= user.totp_pending_until
                else None
            )
            if step is None:
                recent.append(self._clock())
                raise Forbidden("invalid or expired authentication code")
            user.totp_secret, user.totp_pending = user.totp_pending, ""
            user.totp_last_step = step
            codes = [secrets.token_hex(8).upper() for _ in range(10)]
            user.recovery_hashes = [_key(code) for code in codes]
            self._end_sessions(name)
            self._save()
            return codes

    def disable_2fa(self, name: str, password: str, code: str):
        with self._lock:
            user = self.user(name)
            recent = self._reauth(user, password)
            if self.require_2fa or user.require_totp:
                raise Forbidden("2FA is required by your administrator")
            if not user.totp_secret or not self._second_factor(user, code):
                recent.append(self._clock())
                raise Forbidden("invalid authentication code")
            self.reset_2fa(name)

    def reset_2fa(self, name: str):
        with self._lock:
            user = self.user(name)
            user.totp_secret = user.totp_pending = ""
            user.recovery_hashes = []
            user.totp_last_step = -1
            self._end_sessions(name)
            self._save()

    def enforce_2fa(self, enabled: bool):
        with self._lock:
            self.require_2fa = enabled
            # Password-only sessions are immediately restricted to enrollment.
            self._save()

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
