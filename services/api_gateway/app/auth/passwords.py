import secrets
import string

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=2, type=Type.ID)
_COMMON_PASSWORDS = frozenset(
    {
        "123456789012",
        "password1234",
        "password123!",
        "qwerty123456",
        "letmein12345",
        "welcome12345",
        "admin1234567",
    }
)


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str, username: str = "") -> None:
    if len(password) < 12:
        raise PasswordPolicyError("Password must contain at least 12 characters")
    if len(password) > 128:
        raise PasswordPolicyError("Password must contain no more than 128 characters")
    if sum(
        (
            any(char.islower() for char in password),
            any(char.isupper() for char in password),
            any(char.isdigit() for char in password),
            any(not char.isalnum() for char in password),
        )
    ) < 3:
        raise PasswordPolicyError("Password must use at least three character classes")
    normalized = password.casefold()
    if normalized in _COMMON_PASSWORDS:
        raise PasswordPolicyError("Choose a less common password")
    if len(username) >= 3 and username.casefold() in normalized:
        raise PasswordPolicyError("Password must not contain the username")


def hash_password(password: str, username: str = "") -> str:
    validate_password(password, username)
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def generate_temporary_password(username: str = "") -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        try:
            validate_password(password, username)
        except PasswordPolicyError:
            continue
        return password
