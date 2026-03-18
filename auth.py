import bcrypt
import re
from typing import Optional, Tuple
import database as db


def hash_password(password: str) -> str:
    salt   = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def validate_username(username: str) -> Tuple[bool, str]:
    if len(username) < 3:
        return False, "Username must be at least 3 characters long"
    if len(username) > 30:
        return False, "Username must be at most 30 characters long"
    if not re.match(r"^[a-zA-Z0-9_]+$", username):
        return False, "Username can only contain letters, numbers, and underscores"
    if db.get_user_by_username(username):
        return False, "Username already taken"
    return True, ""


def validate_email(email: str) -> Tuple[bool, str]:
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, email):
        return False, "Invalid email format"
    if db.get_user_by_email(email):
        return False, "Email already registered"
    return True, ""


def validate_password(password: str) -> Tuple[bool, str]:
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if len(password) > 128:
        return False, "Password must be at most 128 characters long"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one digit"
    return True, ""


def register_user(username: str, email: str, password: str) -> Tuple[bool, str, Optional[int]]:
    valid, msg = validate_username(username)
    if not valid:
        return False, msg, None

    valid, msg = validate_email(email)
    if not valid:
        return False, msg, None

    valid, msg = validate_password(password)
    if not valid:
        return False, msg, None

    password_hash = hash_password(password)
    user_id       = db.create_user(username, email, password_hash)
    if user_id:
        return True, "User registered successfully!", user_id
    return False, "Failed to create user — please try again.", None


def login_user(username: str, password: str) -> Tuple[bool, str, Optional[int]]:
    """
    Authenticate a user.

    Changed from original: we no longer block login if Gmail/Drive are not
    connected.  Instead we let the user in and the main app sidebar shows a
    'Reconnect Services' button when the connection is absent.  This prevents
    users from being permanently locked out after a failed or expired OAuth.
    """
    user = db.get_user_by_username(username)
    if not user:
        return False, "Invalid username or password", None

    if not verify_password(password, user["password_hash"]):
        return False, "Invalid username or password", None

    db.update_last_login(user["id"])
    return True, "Login successful!", user["id"]


def create_user_session(user_id: int) -> str:
    return db.create_session(user_id, session_duration_hours=24)


def validate_user_session(session_token: str) -> Optional[int]:
    return db.validate_session(session_token)


def logout_user(session_token: str):
    db.delete_session(session_token)


def get_user_info(user_id: int) -> Optional[dict]:
    return db.get_user_by_id(user_id)


if __name__ == "__main__":
    db.initialize_database()
    print("Testing authentication...")

    success, msg, user_id = register_user("testuser", "test@example.com", "TestPass123")
    print(f"Registration: {msg} (ID: {user_id})")

    success, msg, user_id = login_user("testuser", "TestPass123")
    print(f"Login (no OAuth): {msg}")   # should now succeed

    if success:
        token = create_user_session(user_id)
        print(f"Session token: {token[:20]}...")
        uid   = validate_user_session(token)
        print(f"Validated user: {uid}")
        logout_user(token)
        print("Logged out.")
        print(f"After logout: {validate_user_session(token)}")
