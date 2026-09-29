from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .util import atomic_write


DEVICE_KEY_NAME = "device.key"


class KeyErrorSafe(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)


# ---------------------------------------------------------------------------
# Device key
# ---------------------------------------------------------------------------

def app_data_dir() -> Path:
    override = os.environ.get("ANONYMIZER_HOME")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / "Anonymizer"
    path.mkdir(parents=True, exist_ok=True)
    return path


def device_key_path() -> Path:
    return app_data_dir() / DEVICE_KEY_NAME


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Bind the device key to the current Windows account via DPAPI.

    Nothing leaves the machine and nothing has to be typed: Windows itself holds the
    master secret and only unwraps for this user profile.
    """
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(payload: bytes) -> Blob:
        buffer = ctypes.create_string_buffer(payload, len(payload))
        return Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, result = to_blob(data), Blob()
    entropy = to_blob(b"Anonymizer/device-key/v2")
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(result)):
        raise KeyErrorSafe("Windows отклонил операцию с ключом устройства (DPAPI)")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def _read_device_key(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(b"DPAPI1:"):
        if os.name != "nt":
            raise KeyErrorSafe("Ключ устройства защищён Windows DPAPI и читается только на Windows")
        return _dpapi(raw[7:], protect=False)
    if raw.startswith(b"RAW1:"):
        return raw[5:]
    raise KeyErrorSafe("Неизвестный формат ключа устройства")


def load_device_key(create: bool = True) -> bytes:
    path = device_key_path()
    if path.exists():
        return _read_device_key(path)
    if not create:
        raise KeyErrorSafe(
            "Ключ устройства не найден. Восстановление возможно только на компьютере и под учётной записью, "
            "где выполнялось обезличивание, либо после возврата резервной копии ключа устройства."
        )
    key = secrets.token_bytes(32)
    if os.name == "nt":
        atomic_write(path, b"DPAPI1:" + _dpapi(key, protect=True))
    else:
        atomic_write(path, b"RAW1:" + key)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return key


def export_device_key(destination: Path) -> Path:
    """Copy the device key so a reinstall does not strand every restore key ever produced."""
    source = device_key_path()
    if not source.exists():
        load_device_key()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / DEVICE_KEY_NAME
    shutil.copy2(source, target)
    return target


def import_device_key(source: Path) -> None:
    _read_device_key(source)
    atomic_write(device_key_path(), source.read_bytes())


# ---------------------------------------------------------------------------
# Хранилище соответствий: один зашифрованный файл
# ---------------------------------------------------------------------------

VAULT_MAGIC = b"ANONVAULT1"


def encrypt_blob(data: bytes, key: bytes | None = None) -> bytes:
    """AES-256-GCM под ключом устройства. Формат: магическая строка, nonce, шифртекст с тегом."""
    key = key or load_device_key()
    nonce = os.urandom(12)
    return VAULT_MAGIC + nonce + AESGCM(key).encrypt(nonce, data, VAULT_MAGIC)


def decrypt_blob(blob: bytes, key: bytes | None = None) -> bytes:
    if not blob.startswith(VAULT_MAGIC) or len(blob) < len(VAULT_MAGIC) + 12 + 16:
        raise KeyErrorSafe("Файл хранилища повреждён или создан другой программой")
    key = key or load_device_key(create=False)
    nonce = blob[len(VAULT_MAGIC):len(VAULT_MAGIC) + 12]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(VAULT_MAGIC) + 12:], VAULT_MAGIC)
    except Exception as exc:
        raise KeyErrorSafe(
            "Ключ устройства не подходит к хранилищу. Оно создано на другом компьютере или под другой "
            "учётной записью, либо ключ устройства был удалён."
        ) from exc


def password_encrypt(data: bytes, password: str) -> bytes:
    """Переносимая копия: ключ выводится из пароля (scrypt), поэтому файл читается на любом компьютере."""
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, dklen=32, maxmem=2 ** 27)
    return b"ANONBACKUP1" + salt + nonce + AESGCM(key).encrypt(nonce, data, b"ANONBACKUP1")


def password_decrypt(blob: bytes, password: str) -> bytes:
    magic = b"ANONBACKUP1"
    if not blob.startswith(magic) or len(blob) < len(magic) + 28 + 16:
        raise KeyErrorSafe("Это не файл резервной копии Anonymizer")
    salt = blob[len(magic):len(magic) + 16]
    nonce = blob[len(magic) + 16:len(magic) + 28]
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, dklen=32, maxmem=2 ** 27)
    try:
        return AESGCM(key).decrypt(nonce, blob[len(magic) + 28:], magic)
    except Exception as exc:
        raise KeyErrorSafe("Неверный пароль или повреждённый файл резервной копии") from exc
