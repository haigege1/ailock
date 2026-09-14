"""口令安全：哈希、恢复码、失败冷却。

口令永不以明文形式存在于磁盘：
  hash        PBKDF2-HMAC-SHA256，20 万次迭代，随机 16 字节盐
  存储加密    Windows DPAPI（CryptProtectData），仅限当前用户当前机器可解
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import threading
import time
from pathlib import Path

from .config import app_dir

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
HASH_BYTES = 32

VAULT_NAME = "vault.dat"


# ---------- DPAPI ----------

def _dpapi_protect(data: bytes) -> bytes:
    try:
        import win32crypt
        return win32crypt.CryptProtectData(data, "AiLockVault", None, None, None, 0)
    except Exception:
        # 没有 pywin32 时退化为「仅混淆」，至少不再是肉眼可读的明文
        return base64.b64encode(data)


def _dpapi_unprotect(blob: bytes) -> bytes:
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
    except Exception:
        try:
            return base64.b64decode(blob)
        except Exception:
            return b""


# ---------- 哈希 ----------

def _hash(password: str, salt: bytes, iterations: int) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=HASH_BYTES
    )
    return base64.b64encode(dk).decode("ascii")


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def verify_password(password: str, record: dict) -> bool:
    """record: {"hash":..., "salt_b64":..., "iterations":...}"""
    try:
        salt = base64.b64decode(record["salt_b64"])
        iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
        expected = record["hash"]
    except Exception:
        return False
    actual = _hash(password, salt, iterations)
    return hmac.compare_digest(actual, expected)


def make_record(password: str) -> dict:
    salt = new_salt()
    return {
        "hash": _hash(password, salt, PBKDF2_ITERATIONS),
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "iterations": PBKDF2_ITERATIONS,
        "updated": time.time(),
    }


def generate_recovery_code() -> str:
    """12 位恢复码，去掉易混淆字符，分成两段便于抄写。"""
    alphabet = "".join(c for c in (string.ascii_uppercase + string.digits)
                       if c not in "O0I1L")
    body = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{body[:6]}-{body[6:]}"


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


# ---------- 保险箱 ----------

class Vault:
    """DPAPI 加密的口令保险箱。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_dir() / VAULT_NAME
        self._lock = threading.RLock()
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            blob = self.path.read_bytes()
            plain = _dpapi_unprotect(blob)
            data = json.loads(plain.decode("utf-8"))
            if isinstance(data, dict):
                self._data = data
        except FileNotFoundError:
            self._data = {}
        except Exception:
            self._data = {}

    def _save(self) -> None:
        blob = _dpapi_protect(
            json.dumps(self._data, ensure_ascii=False).encode("utf-8")
        )
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)

    @property
    def is_set(self) -> bool:
        return bool(self._data.get("password"))

    def set_password(self, password: str) -> None:
        with self._lock:
            self._data["password"] = make_record(password)
            self._save()

    def verify(self, password: str) -> bool:
        with self._lock:
            rec = self._data.get("password")
        if not rec:
            return False
        return verify_password(password, rec)

    # ---- 恢复码 ----
    def has_recovery(self) -> bool:
        return bool(self._data.get("recovery"))

    def set_recovery(self, code: str) -> None:
        with self._lock:
            self._data["recovery"] = make_record(normalize_code(code))
            self._save()

    def verify_recovery(self, code: str) -> bool:
        with self._lock:
            rec = self._data.get("recovery")
        if not rec:
            return False
        return verify_password(normalize_code(code), rec)

    def ensure_recovery(self) -> str:
        """没有恢复码时生成一个并返回；已有则抛异常（避免覆盖用户已保存的）。"""
        if self.has_recovery():
            raise RuntimeError("recovery code already exists")
        code = generate_recovery_code()
        self.set_recovery(code)
        return code

    def regenerate_recovery(self) -> str:
        code = generate_recovery_code()
        self.set_recovery(code)
        return code

    def reset_all(self) -> None:
        """清空口令与恢复码（忘记密码时的最后手段）。"""
        with self._lock:
            self._data = {}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


# ---------- 失败冷却 ----------

class Throttle:
    """错误次数累计到阈值后进入冷却期。"""

    def __init__(self, state, max_attempts: int = 5, cooldown_seconds: int = 30):
        self.state = state
        self.max_attempts = max(1, int(max_attempts))
        self.cooldown_seconds = max(0, int(cooldown_seconds))

    def remaining_seconds(self) -> float:
        return max(0.0, self.state.cooldown_until - time.time())

    def in_cooldown(self) -> bool:
        return self.remaining_seconds() > 0

    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.state.failed_attempts)

    def record_failure(self) -> None:
        self.state.failed_attempts += 1
        if self.state.failed_attempts >= self.max_attempts:
            self.state.failed_attempts = 0
            self.state.cooldown_until = time.time() + self.cooldown_seconds
        self.state._save()

    def record_success(self) -> None:
        self.state.reset()
