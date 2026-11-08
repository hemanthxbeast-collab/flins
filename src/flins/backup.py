"""Encrypted backups (SPEC §14 M9).

A backup is one file: your database (a consistent snapshot, taken with SQLite's backup API while
FLINS runs) plus your personal config files, zipped, then encrypted with AES-256-GCM. The key
comes from your passphrase (`FLINS_BACKUP_PASSPHRASE` in .env) through scrypt. `.env` itself is
never included. Restoring writes to a new folder and never overwrites anything.

File layout: b"FLINSBK1" | salt (16) | nonce (12) | ciphertext+tag. The header is authenticated.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"FLINSBK1"
SUFFIX = ".flinsbak"
MIN_PASSPHRASE = 12
SCRYPT_N = 2**15
# Personal files worth keeping (all git-ignored). Never .env: the passphrase lives there.
CONFIG_FILES = (
    "config/flins.yaml",
    "config/policy.yaml",
    "config/about_me.yaml",
    "config/tone_profiles.yaml",
    "config/contacts.yaml",
    "config/faq.yaml",
    "skills/skills.lock",
)


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    files: list[str]
    removed: list[Path]


def _key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"the backup passphrase must be at least {MIN_PASSPHRASE} characters")
    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(data: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    return header + AESGCM(_key(passphrase, salt)).encrypt(nonce, data, MAGIC + salt)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 28 + 16:
        raise BackupError("not a FLINS backup file")
    salt = blob[8:24]
    nonce = blob[24:36]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, blob[36:], MAGIC + salt)
    except InvalidTag:
        raise BackupError("wrong passphrase, or the file was changed or damaged") from None


def _snapshot(db_path: Path) -> bytes:
    """A consistent copy of a live WAL database."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "flins.db"
        src = sqlite3.connect(db_path)  # reading only; WAL needs a normal connection
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return copy.read_bytes()


def create_backup(
    root: Path,
    db_path: Path,
    out_dir: Path,
    passphrase: str,
    now: datetime,
    *,
    keep: int = 14,
) -> BackupResult:
    if not db_path.is_file():
        raise BackupError(f"no database at {db_path}")
    _key(passphrase, b"0" * 16)  # check the passphrase before doing any work
    buf = io.BytesIO()
    names = ["flins.db"]
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("flins.db", _snapshot(db_path))
        for rel in CONFIG_FILES:
            p = root / rel
            if p.is_file():
                z.write(p, rel)
                names.append(rel)
        z.writestr("FLINS-BACKUP.txt", f"FLINS backup made {now.isoformat()}\n")
    blob = encrypt(buf.getvalue(), passphrase)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"flins-{now.strftime('%Y%m%d-%H%M%S')}{SUFFIX}"
    tmp = path.with_suffix(".partial")
    tmp.write_bytes(blob)
    tmp.replace(path)
    removed = prune(out_dir, keep)
    return BackupResult(path, len(blob), names, removed)


def prune(out_dir: Path, keep: int) -> list[Path]:
    """Keep the newest `keep` backups (by name, which sorts by time)."""
    files = sorted(out_dir.glob(f"flins-*{SUFFIX}"))
    old = files[:-keep] if keep > 0 and len(files) > keep else []
    for f in old:
        f.unlink(missing_ok=True)
    return old


def latest(out_dir: Path) -> Path | None:
    files = sorted(out_dir.glob(f"flins-*{SUFFIX}")) if out_dir.is_dir() else []
    return files[-1] if files else None


def restore_backup(path: Path, passphrase: str, dest: Path) -> list[str]:
    """Decrypt into a new folder `dest`. Never overwrites: `dest` must not exist yet."""
    if dest.exists():
        raise BackupError(f"{dest} already exists; pick a new folder")
    data = decrypt(path.read_bytes(), passphrase)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        for n in names:
            if n.startswith(("/", "\\")) or ".." in Path(n).parts or ":" in n:
                raise BackupError(f"unexpected path in backup: {n!r}")
        dest.mkdir(parents=True)
        z.extractall(dest)
    db = dest / "flins.db"
    con = sqlite3.connect(db)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if ok != "ok":
        raise BackupError(f"restored database failed its integrity check: {ok}")
    return names
