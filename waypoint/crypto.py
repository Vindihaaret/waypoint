"""Kryptering med gpg (OpenPGP, AES-256). Valgt fordi gpg finnes på alle Linux-systemer, og fordi en
kryptert sikkerhetskopi da kan åpnes uten Waypoint:  gpg -d waypoint-….tar.gz.gpg | tar xz

gpg kjøres med en egen, midlertidig GNUPGHOME, så brukerens egne nøkler og gpg-agent ikke blir berørt
(viktig når ~/.gnupg selv gjenopprettes), og passordet sendes gjennom en egen filbeskrivelse."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MIN_LENGTH = 8


class BadPassword(Exception):
    pass


class CryptoError(Exception):
    pass


def available() -> bool:
    return shutil.which("gpg") is not None


def is_encrypted(path: Path) -> bool:
    """Arkivet er gzip (1f 8b) når det ikke er kryptert."""
    try:
        with open(path, "rb") as f:
            return f.read(2) != b"\x1f\x8b"
    except OSError:
        return False


class _Gpg:
    """Én gpg-prosess. Bruk som kontekst: with _Gpg(...) as g: g.proc.stdin / g.proc.stdout."""

    def __init__(self, args: list[str], password: str, stdin=None, stdout=None):
        if not available():
            raise CryptoError("gpg mangler")
        from . import debuglog
        debuglog.add_secret(password)
        self.home = tempfile.mkdtemp(prefix="waypoint-gpg-")
        os.chmod(self.home, 0o700)
        r, w = os.pipe()
        os.write(w, password.encode() + b"\n")
        os.close(w)
        self.proc = subprocess.Popen(
            ["gpg", "--homedir", self.home, "--batch", "--yes", "--quiet", "--no-symkey-cache",
             "--pinentry-mode", "loopback", "--passphrase-fd", str(r), *args],
            stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, pass_fds=(r,))
        os.close(r)

    def finish(self) -> tuple[int, str]:
        if self.proc.stdin and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        err = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
        code = self.proc.wait()
        return code, err

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def cleanup(self):
        subprocess.run(["gpgconf", "--homedir", self.home, "--kill", "gpg-agent"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.home, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.proc.poll() is None:  # aldri la en gpg-prosess bli igjen, heller ikke uten unntak
            self.kill()
        self.cleanup()


def encrypt_to(dest: Path, password: str) -> _Gpg:
    """Starter kryptering til dest; skriv klartekst til .proc.stdin og kall .finish()."""
    return _Gpg(["--symmetric", "--cipher-algo", "AES256", "--s2k-digest-algo", "SHA512", "--s2k-mode", "3",
                 "--s2k-count", "65011712", "--compress-algo", "none", "--output", str(dest)],
                password, stdin=subprocess.PIPE)


def decrypt_from(src: Path, password: str) -> _Gpg:
    """Starter dekryptering av src; les klartekst fra .proc.stdout og kall .finish()."""
    return _Gpg(["--decrypt", str(src)], password, stdout=subprocess.PIPE)


def check_password(src: Path, password: str) -> bool:
    """Prøver å dekryptere begynnelsen av filen."""
    with decrypt_from(src, password) as g:
        data = g.proc.stdout.read(64)
        if data:
            g.kill()
            return True
        code, _err = g.finish()
        return code == 0


def raise_for(code: int, err: str):
    if code == 0:
        return
    low = err.lower()
    if "bad session key" in low or "bad passphrase" in low or "decryption failed" in low:
        raise BadPassword()
    raise CryptoError(err.strip() or f"gpg avsluttet med kode {code}")
