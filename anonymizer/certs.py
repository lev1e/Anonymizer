"""Сертификат для локальной страницы add-in.

Office открывает add-in только по HTTPS. Поэтому программа создаёт собственный корневой сертификат, который может
подписывать только `localhost` и `127.0.0.1` (ограничение имён), и сертификат сервера от него. Корневой сертификат
ставится в доверенные для текущего пользователя (без прав администратора). Закрытые ключи лежат в профиле программы
и зашифрованы ключом устройства, как и хранилище.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import ssl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import crypto

CA_NAME = "Anonymizer local add-in CA"
LEAF_DAYS = 397
CA_DAYS = 3650
RENEW_BEFORE_DAYS = 45


@dataclass
class CertFiles:
    ca_cert: Path
    ca_key: Path
    leaf_cert: Path
    leaf_key: Path
    password: bytes
    ca_created: bool = False
    leaf_created: bool = False


def addin_dir() -> Path:
    path = crypto.app_data_dir() / "addin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _password(key: bytes) -> bytes:
    return hashlib.sha256(b"anonymizer/addin-tls/v1" + key).hexdigest().encode("ascii")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _pem_key(key, password: bytes) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.BestAvailableEncryption(password))


def _load_key(path: Path, password: bytes):
    return serialization.load_pem_private_key(path.read_bytes(), password=password)


def _expires_soon(cert_path: Path, days: int, now: dt.datetime) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return cert.not_valid_after_utc - now < dt.timedelta(days=days)
    except Exception:
        return True


def _make_ca(now: dt.datetime):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Anonymizer")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(
                permitted_subtrees=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_network("127.0.0.1/32"))],
                excluded_subtrees=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _make_leaf(ca_key, ca_cert: x509.Certificate, now: dt.datetime):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def ensure_certs(directory: Path | None = None, key: bytes | None = None, now: dt.datetime | None = None) -> CertFiles:
    """Создаёт недостающее. Корневой сертификат живёт долго, сертификат сервера обновляется сам (без нового доверия)."""
    directory = directory or addin_dir()
    directory.mkdir(parents=True, exist_ok=True)
    password = _password(key if key is not None else crypto.load_device_key())
    now = now or _utc_now()
    files = CertFiles(directory / "ca.crt", directory / "ca.key", directory / "localhost.crt", directory / "localhost.key", password)
    if not (files.ca_cert.exists() and files.ca_key.exists()) or _expires_soon(files.ca_cert, 365, now):
        ca_key, ca_cert = _make_ca(now)
        _write_private(files.ca_key, _pem_key(ca_key, password))
        files.ca_cert.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        files.ca_created = True
    if files.ca_created or not (files.leaf_cert.exists() and files.leaf_key.exists()) or _expires_soon(files.leaf_cert, RENEW_BEFORE_DAYS, now):
        ca_key = _load_key(files.ca_key, password)
        ca_cert = x509.load_pem_x509_certificate(files.ca_cert.read_bytes())
        leaf_key, leaf_cert = _make_leaf(ca_key, ca_cert, now)
        _write_private(files.leaf_key, _pem_key(leaf_key, password))
        files.leaf_cert.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
        files.leaf_created = True
    return files


def ssl_context(files: CertFiles) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(files.leaf_cert), str(files.leaf_key), password=files.password.decode("ascii"))
    return context


# -- доверие -------------------------------------------------------------------------

def _run(command: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def login_keychain() -> str:
    return str(Path.home() / "Library" / "Keychains" / "login.keychain-db")


def is_trusted(files: CertFiles) -> bool:
    """Доверяет ли система сертификату сервера. Нужна, чтобы не показывать окно подтверждения без необходимости."""
    try:
        if sys.platform == "darwin":
            return _run(["security", "verify-cert", "-c", str(files.leaf_cert), "-p", "ssl", "-s", "localhost"], 30).returncode == 0
        if sys.platform == "win32":
            cert = x509.load_pem_x509_certificate(files.ca_cert.read_bytes())
            thumb = cert.fingerprint(hashes.SHA1()).hex().upper()
            out = _run(["certutil", "-user", "-store", "Root", thumb], 30)
            return out.returncode == 0 and thumb.lower() in out.stdout.lower().replace(" ", "")
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def trust_ca(files: CertFiles) -> tuple[bool, str]:
    """Добавляет корневой сертификат в доверенные для пользователя. Система сама спросит подтверждение."""
    try:
        if sys.platform == "darwin":
            out = _run(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl", "-k", login_keychain(), str(files.ca_cert)])
        elif sys.platform == "win32":
            out = _run(["certutil", "-user", "-addstore", "-f", "Root", str(files.ca_cert)])
        else:
            return False, "На этой системе доверие к сертификату настраивается вручную."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Не удалось добавить сертификат в доверенные ({type(exc).__name__})."
    if out.returncode != 0:
        return False, "Система не добавила сертификат в доверенные (подтверждение отклонено или нет доступа)."
    return True, "Сертификат добавлен в доверенные для текущего пользователя."


def untrust_ca(files: CertFiles) -> None:
    try:
        if sys.platform == "darwin":
            _run(["security", "remove-trusted-cert", str(files.ca_cert)], 60)
        elif sys.platform == "win32":
            cert = x509.load_pem_x509_certificate(files.ca_cert.read_bytes())
            _run(["certutil", "-user", "-delstore", "Root", cert.fingerprint(hashes.SHA1()).hex().upper()], 60)
    except (OSError, subprocess.SubprocessError):
        pass
