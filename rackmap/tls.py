"""rackmap's own HTTPS certificate.

Consoles carry passwords, so rackmap serves HTTPS by default. With nothing configured it makes
its own certificate the first time it starts. Browsers can't verify a home-made certificate and
will warn once; the connection is encrypted all the same. To avoid the warning, give rackmap a
real certificate (RACKMAP_TLS_CERT and RACKMAP_TLS_KEY) or put it behind a proxy that has one.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VALID_DAYS = 825
RENEW_BEFORE_DAYS = 30


def _own_addresses() -> list[str]:
    found = {"127.0.0.1"}
    try:  # the address this machine uses to reach others; nothing is actually sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(found)


def _make(cert_path: Path, key_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "rackmap"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])])
    names: list[x509.GeneralName] = [x509.DNSName(n) for n in dict.fromkeys([host, "localhost", "rackmap"])]
    names += [x509.IPAddress(ipaddress.ip_address(a)) for a in _own_addresses()]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.touch(mode=0o600, exist_ok=True)
    os.chmod(key_path, 0o600)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def own_certificate(data_dir: Path) -> tuple[Path, Path]:
    """The (certificate, key) rackmap made for itself, made or renewed as needed."""
    cert_path, key_path = data_dir / "tls-cert.pem", data_dir / "tls-key.pem"
    fresh = False
    if cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            fresh = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) > datetime.timedelta(days=RENEW_BEFORE_DAYS)
        except ValueError:
            fresh = False
    if not fresh:
        data_dir.mkdir(parents=True, exist_ok=True)
        _make(cert_path, key_path)
    return cert_path, key_path
