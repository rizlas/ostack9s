"""Privacy mode: mask sensitive values on screen (for demos and screencasts).

When enabled, every string shown by the UI goes through :func:`mask`, which
replaces public IP addresses, e-mail addresses, UUIDs and other identifiers,
MAC addresses, fingerprints, SSH keys, PEM blocks, tokens in URLs, secret
values and user supplied words. Replacements are consistent within a session
(the same value always gets the same placeholder), so the screen stays
readable. Private addresses (e.g. 10.0.0.0/8) are left untouched. Data sent to
OpenStack is never altered, only what is displayed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from collections.abc import Iterable

_enabled = False
_salt = secrets.token_hex(8)
_maps: dict[str, dict[str, str]] = {}
_names: dict[str, str] = {}
_words: list[str] = []

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")
IPV6 = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![0-9A-Fa-f:])"
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
HEX_ID = re.compile(r"\b[0-9a-f]{32,64}\b")
MAC = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b")
FINGERPRINT = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){15,}\b|SHA256:[A-Za-z0-9+/=]{20,}")
SSH_KEY = re.compile(r"\b(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-\S+) [A-Za-z0-9+/=]{20,}")
PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)
URL_TOKEN = re.compile(r"([?&](?:token|sig|signature|temp_url_sig)=)[^&\s\"']+", re.IGNORECASE)
SECRET_VALUE = re.compile(
    r"((?:secret|password|passwd|private_key|admin_pass|adminPass)[\w-]*[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',}]+)",
    re.IGNORECASE,
)


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def is_enabled() -> bool:
    return _enabled


def add_words(words: Iterable[str]) -> None:
    """Extra words to hide (e.g. a surname that appears in resource names)."""
    for word in words:
        if word and word not in _words:
            _words.append(word)
    _words.sort(key=len, reverse=True)


def register_names(kind: str, names: Iterable[str]) -> None:
    """Map known sensitive names (projects, users) to stable aliases."""
    for name in names:
        if name and name not in _names:
            count = sum(1 for alias in _names.values() if alias.startswith(kind))
            _names[name] = f"{kind}-{count + 1}"
    add_words(_names)


def _alias(kind: str, value: str, make) -> str:
    table = _maps.setdefault(kind, {})
    if value not in table:
        table[value] = make(len(table) + 1, value)
    return table[value]


def _fake_hex(value: str, length: int) -> str:
    digest = hashlib.sha256((_salt + value).encode()).hexdigest()
    while len(digest) < length:
        digest += hashlib.sha256(digest.encode()).hexdigest()
    return digest[:length]


def _ipv4(match: re.Match[str]) -> str:
    text = match.group(0)
    addr, _, prefix = text.partition("/")
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return text
    if not ip.is_global:
        return text
    fake = _alias("ipv4", addr, lambda n, _v: f"203.0.113.{n % 254 or 254}")
    return f"{fake}/{prefix}" if prefix else fake


def _ipv6(match: re.Match[str]) -> str:
    text = match.group(0)
    addr, _, prefix = text.partition("/")
    try:
        ip = ipaddress.IPv6Address(addr)
    except ValueError:
        return text
    if not ip.is_global:
        return text
    fake = _alias("ipv6", addr, lambda n, _v: f"2001:db8::{n:x}")
    return f"{fake}/{prefix}" if prefix else fake


def _uuid(match: re.Match[str]) -> str:
    h = _fake_hex(match.group(0).lower(), 32)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def mask(text: str) -> str:
    """Masked copy of ``text`` when privacy mode is on, ``text`` otherwise."""
    if not _enabled or not text:
        return text
    out = PEM.sub("[hidden key]", text)
    out = SSH_KEY.sub(lambda m: f"{m.group(1)} [hidden]", out)
    out = SECRET_VALUE.sub(lambda m: f"{m.group(1)}***", out)
    out = URL_TOKEN.sub(lambda m: f"{m.group(1)}***", out)
    out = EMAIL.sub(
        lambda m: _alias("email", m.group(0), lambda n, _v: f"user{n}@example.org"), out
    )
    out = FINGERPRINT.sub("[hidden fingerprint]", out)
    out = MAC.sub(
        lambda m: _alias("mac", m.group(0).lower(), lambda n, _v: f"00:00:5e:00:53:{n % 256:02x}"),
        out,
    )
    out = UUID.sub(_uuid, out)
    out = HEX_ID.sub(lambda m: _fake_hex(m.group(0), len(m.group(0))), out)
    out = IPV4.sub(_ipv4, out)
    out = IPV6.sub(_ipv6, out)
    for word in _words:
        if word in out:
            out = out.replace(word, _names.get(word, "▒" * min(len(word), 6)))
    return out


def reset() -> None:
    """Forget every mapping (used by tests)."""
    global _enabled
    _enabled = False
    _maps.clear()
    _names.clear()
    _words.clear()
