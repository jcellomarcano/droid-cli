"""Anonimiza los fixtures crudos de tests/fixtures/raw/ hacia tests/fixtures/.

Reemplaza paquetes de terceros, direcciones IPv4/IPv6 (incluidas sus formas hex
en /proc/net/{tcp,tcp6,udp,udp6}, columna local Y remota), MACs, emails,
seriales/SSID/androidId y rutas ~/ de bases de datos. Todo lo demas se copia
byte a byte. Determinista por token: el mismo valor real siempre produce el
mismo placeholder (derivado de un hash estable), sin importar que otros
ficheros esten presentes en el corpus ni el orden en que aparezcan.

Ademas escribe tests/fixtures/MANIFEST.txt (ver `write_manifest`).
"""
import hashlib
import ipaddress
import re
from pathlib import Path
from typing import Dict, List, Tuple

RAW_DIR = Path(__file__).parent / "fixtures" / "raw"
OUT_DIR = Path(__file__).parent / "fixtures"
SOURCES_FILE = RAW_DIR / "SOURCES.txt"
MANIFEST_FILE = OUT_DIR / "MANIFEST.txt"

PRIVATE_TERMS_FILE = RAW_DIR / "PRIVATE_TERMS.txt"


def load_private_terms(path: Path = PRIVATE_TERMS_FILE) -> Tuple[Tuple[Tuple[str, str], ...], Tuple[str, ...]]:
    """Lee los terminos privados del propietario desde un fichero ignorado por git:
    lineas `map<TAB>paquete_real<TAB>placeholder` y `forbid<TAB>subcadena`."""
    maps: List[Tuple[str, str]] = []
    forbid: List[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.rstrip("\n").split("\t")
            if not parts or parts[0].startswith("#"):
                continue
            if parts[0] == "map" and len(parts) == 3:
                maps.append((parts[1].strip(), parts[2].strip()))
            elif parts[0] == "forbid" and len(parts) == 2:
                forbid.append(parts[1].strip())
            elif parts[0] == "word" and len(parts) == 3:
                WORD_MAP[parts[1].strip()] = parts[2].strip()
    return tuple(maps), tuple(forbid)


WORD_MAP: Dict[str, str] = {}
FIXED_PKG_MAP: Tuple[Tuple[str, str], ...] = ()
FIXED_PKG_PREFIX_MAP: Dict[str, str] = {}
FIXED_PKG_PREFIXES: Tuple[str, ...] = ()
FIXED_TOKEN_RE = None
COMM_MAP: Dict[str, str] = {}
FORBIDDEN_SUBSTRINGS: Tuple[str, ...] = ("@gmail",)


def configure(maps: Tuple[Tuple[str, str], ...], forbid: Tuple[str, ...]) -> None:
    """Rebinds the private tables: fixed package mappings, their 15-character
    process-name tails (the `comm` truncation of /proc) and the forbidden substrings."""
    global FIXED_PKG_MAP, FIXED_PKG_PREFIX_MAP, FIXED_PKG_PREFIXES, FIXED_TOKEN_RE, COMM_MAP, FORBIDDEN_SUBSTRINGS
    FIXED_PKG_MAP = tuple(maps)
    FIXED_PKG_PREFIX_MAP = {".".join(literal.split(".")[:2]): repl for literal, repl in FIXED_PKG_MAP}
    FIXED_PKG_PREFIXES = tuple(FIXED_PKG_PREFIX_MAP)
    FIXED_TOKEN_RE = re.compile(
        r"\b(" + "|".join(re.escape(p) for p in FIXED_PKG_PREFIXES) + r")(?:\.[a-z0-9_]+)*\b"
    ) if FIXED_PKG_PREFIXES else None
    COMM_MAP = {literal[-15:]: repl[-15:] for literal, repl in FIXED_PKG_MAP if len(literal) > 15}
    FORBIDDEN_SUBSTRINGS = ("@gmail",) + tuple(forbid) + tuple(literal for literal, _ in FIXED_PKG_MAP) + tuple(WORD_MAP)


configure(*load_private_terms())

PKG_ALLOWLIST_PREFIXES = (
    "android.", "androidx.", "com.android.", "com.google.", "com.qualcomm.",
    "com.samsung.android.", "org.chromium.", "java.", "javax.", "kotlin.",
    "kotlinx.", "dalvik.", "libcore.", "sun.", "org.apache.", "org.json",
    "org.xml", "org.w3c", "com.example.",
)

# Cualquier token con forma de paquete reversed-domain (2+ puntos, es decir
# 3+ etiquetas de [a-z0-9_]) es candidato; ya no se exige que la primera
# etiqueta este en una tabla cerrada de TLDs (eso dejaba pasar paquetes reales
# como ru.yandex.searchapp o tv.twitch.android.app).
PKG_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){2,}\b")

IPV4_RE = re.compile(
    r"(?<![A-Za-z0-9._-])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![A-Za-z0-9._-])"
)
# Lineas de /proc/net/{tcp,tcp6,udp,udp6}: "  1: <local_hex>:<port> <remote_hex>:<port> ..."
NET_LINE_RE = re.compile(r"^(\s*\d+:\s+)([0-9A-Fa-f]+:[0-9A-Fa-f]{4})(\s+)([0-9A-Fa-f]+:[0-9A-Fa-f]{4})(\s.*)$")
IPV6_CANDIDATE_RE = re.compile(r"(?<![0-9A-Za-z:.])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(?![0-9A-Za-z:.])")
# MAC delimitada por espacios/inicio-fin de linea (no una porcion de una
# cadena hex:hex... mas larga) y no la MAC nula 00:00:00:00:00:00.
MAC_RE = re.compile(r"(?:(?<=^)|(?<=\s))([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?=$|\s)", re.MULTILINE)
NULL_MAC = "00:00:00:00:00:00"
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,24}")
HOME_PATH_RE = re.compile(r"(database:\s*~)/\S*")
SERIALNO_PROP_RE = re.compile(r"(ro\.serialno\s*[:=]\s*)(\S+)")
SERIALNO_KV_RE = re.compile(r"(\bserialno\s*=\s*)(\S+)")
SERIAL_LABEL_RE = re.compile(r"(\bSerial:\s*)(\S+)")
SSID_RE = re.compile(r"(\bSSID:\s*)(\S+)")
ANDROID_ID_RE = re.compile(r"(\bandroidId\s*[:=]\s*)(\S+)")


ANON_BASE = "203.0.113."
ANON_V6_NET = ipaddress.ip_network("2001:db8::/32")

ALLOWED_V4_NETS = (
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("10.0.2.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("224.0.0.0/4"),
)

# Ventana de contexto (en caracteres, hacia atras desde el inicio del match)
# usada para decidir si una cadena con forma de IPv4 es en realidad un numero
# de version (major.minor.patch.build) y no debe tocarse.
_VERSION_WINDOW = 12


def _is_version_context(text: str, start: int) -> bool:
    window = text[max(0, start - _VERSION_WINDOW):start]
    if re.search(r"(?i)version", window):
        return True
    if re.search(r"(?i)\blib[a-z0-9_]*\s$", window):
        return True
    # 'v' pegada justo antes del numero (v1.2.3.4), sin espacio de por medio.
    if re.search(r"(?<![A-Za-z0-9_])v$", window):
        return True
    return False


def _ip_allowed(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return True
    return any(addr in net for net in ALLOWED_V4_NETS)


def _ipv6_excluded(addr: "ipaddress.IPv6Address") -> bool:
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return True
    if addr.ipv4_mapped is not None:
        return True
    return False


def _encode_v4_hex(ip: str) -> str:
    parts = [int(x) for x in ip.split(".")]
    return bytes(parts)[::-1].hex().upper()


def _encode_v6mapped_hex(ip: str) -> str:
    parts = [int(x) for x in ip.split(".")]
    words = b"\x00" * 10 + b"\xff\xff" + bytes(parts)
    raw = b"".join(words[i:i + 4][::-1] for i in range(0, 16, 4))
    return raw.hex().upper()


def _encode_v6_hex(ip6: str) -> str:
    raw = ipaddress.IPv6Address(ip6).packed
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    return words.hex().upper()


def _decode_hex_addr_ip(ip_hex: str):
    if len(ip_hex) == 8:
        b = bytes.fromhex(ip_hex)[::-1]
        return ".".join(str(x) for x in b)
    if len(ip_hex) == 32:
        raw = bytes.fromhex(ip_hex)
        words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        if words[:12] == b"\x00" * 10 + b"\xff\xff":
            return ".".join(str(x) for x in words[12:])
        return str(ipaddress.IPv6Address(words))
    return None


def _net_line_tokens(line: str):
    """Devuelve (match, local_tok, remote_tok) para una linea de /proc/net/*, o None."""
    m = NET_LINE_RE.match(line)
    if not m:
        return None
    _prefix, local_tok, _sep, remote_tok, _rest = m.groups()
    return m, local_tok, remote_tok


def collect_out_of_range_ips(text: str) -> List[str]:
    found = set()
    for m in IPV4_RE.finditer(text):
        if _is_version_context(text, m.start()):
            continue
        ip = ".".join(m.groups())
        if not _ip_allowed(ip):
            found.add(ip)
    for line in text.splitlines():
        parsed = _net_line_tokens(line)
        if not parsed:
            continue
        _m, local_tok, remote_tok = parsed
        for tok in (local_tok, remote_tok):
            ip_hex, _, _port = tok.partition(":")
            ip = _decode_hex_addr_ip(ip_hex)
            if ip and ":" not in ip and not _ip_allowed(ip):
                found.add(ip)
    return sorted(found)


def _stable_int(value: str, nbytes: int) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[: nbytes * 2], 16)


def build_ip_mapping(all_texts: List[str]) -> Dict[str, str]:
    ips = set()
    for text in all_texts:
        ips.update(collect_out_of_range_ips(text))
    mapping = {}
    for ip in sorted(ips):
        octet = 1 + (_stable_int(ip, 4) % 250)
        mapping[ip] = f"{ANON_BASE}{octet}"
    return mapping


def collect_native_ipv6(text: str) -> List[str]:
    found = set()
    for m in IPV6_CANDIDATE_RE.finditer(text):
        tok = m.group(0)
        if tok.count(":") < 2:
            continue
        try:
            addr = ipaddress.IPv6Address(tok)
        except ValueError:
            continue
        if not _ipv6_excluded(addr):
            found.add(addr.compressed)
    for line in text.splitlines():
        parsed = _net_line_tokens(line)
        if not parsed:
            continue
        _m, local_tok, remote_tok = parsed
        for tok in (local_tok, remote_tok):
            ip_hex, _, _port = tok.partition(":")
            if len(ip_hex) != 32:
                continue
            decoded = _decode_hex_addr_ip(ip_hex)
            if decoded and ":" in decoded:
                addr = ipaddress.IPv6Address(decoded)
                if not _ipv6_excluded(addr):
                    found.add(addr.compressed)
    return sorted(found)


def build_ip6_mapping(all_texts: List[str]) -> Dict[str, str]:
    addrs = set()
    for text in all_texts:
        addrs.update(collect_native_ipv6(text))
    mapping = {}
    for addr in sorted(addrs):
        h = _stable_int(addr, 2)
        mapping[addr] = f"2001:db8::{h:x}"
    return mapping


def collect_macs(text: str) -> List[str]:
    return sorted({m.group(0).lower() for m in MAC_RE.finditer(text)})


def build_mac_mapping(all_texts: List[str]) -> Dict[str, str]:
    macs = set()
    for text in all_texts:
        macs.update(collect_macs(text))
    macs = {m for m in macs if not m.startswith("02:00:00:00:00:") and m != NULL_MAC}
    mapping = {}
    for mac in sorted(macs):
        h = _stable_int(mac, 3)
        mapping[mac] = f"02:00:00:{(h >> 16) & 0xFF:02X}:{(h >> 8) & 0xFF:02X}:{h & 0xFF:02X}"
    return mapping


def _is_pkg_allowlisted(token: str) -> bool:
    return token.startswith(PKG_ALLOWLIST_PREFIXES)


def _is_reversed_domain_pkg(token: str) -> bool:
    labels = token.split(".")
    if len(labels) < 3:
        return False
    return all(re.fullmatch(r"[a-z0-9_]+", label) for label in labels)


def _pkg_prefix(token: str) -> str:
    labels = token.split(".")
    return ".".join(labels[:2])


def build_other_pkg_mapping(all_texts: List[str]) -> Dict[str, str]:
    tokens = set()
    for text in all_texts:
        for m in PKG_RE.finditer(text):
            tok = m.group(0)
            if tok.startswith(FIXED_PKG_PREFIXES):
                continue
            if _is_pkg_allowlisted(tok):
                continue
            if not _is_reversed_domain_pkg(tok):
                continue
            tokens.add(_pkg_prefix(tok))
    mapping = {}
    for prefix in sorted(tokens):
        h = _stable_int(prefix, 3)
        mapping[prefix] = f"com.example.app_{h:06x}"
    return mapping


def _pkg_replacement_for(token: str, other_pkg_map: Dict[str, str]):
    """Placeholder para el token COMPLETO (nunca un str.replace parcial de prefijo),
    o None si el token no debe anonimizarse."""
    if token.startswith("com.example."):
        return None
    if _is_pkg_allowlisted(token):
        return None
    if not _is_reversed_domain_pkg(token):
        return None
    prefix = _pkg_prefix(token)
    if prefix in FIXED_PKG_PREFIX_MAP:
        return FIXED_PKG_PREFIX_MAP[prefix]
    return other_pkg_map.get(prefix)


def _apply_fixed_pkg(text: str) -> str:
    if FIXED_TOKEN_RE is not None:
        text = FIXED_TOKEN_RE.sub(lambda m: FIXED_PKG_PREFIX_MAP[m.group(1)], text)
    for tail, repl in COMM_MAP.items():
        text = re.sub(r"(?<=[\s:(])" + re.escape(tail) + r"(?=[\s:)]|$)", repl, text)
    for word, repl in WORD_MAP.items():
        text = text.replace(word, repl)
    return text


def _apply_pkg_maps(text: str, other_pkg_map: Dict[str, str]) -> str:
    text = _apply_fixed_pkg(text)

    def repl(m):
        tok = m.group(0)
        new = _pkg_replacement_for(tok, other_pkg_map)
        return new if new is not None else tok

    return PKG_RE.sub(repl, text)


def _apply_net_lines(text: str, ip_map: Dict[str, str], ip6_map: Dict[str, str]) -> str:
    lines = text.split("\n")

    def _rewrite_tok(tok: str) -> str:
        ip_hex, _, port = tok.partition(":")
        ip = _decode_hex_addr_ip(ip_hex)
        if ip and ":" not in ip and ip in ip_map:
            new_ip = ip_map[ip]
            new_hex = _encode_v4_hex(new_ip) if len(ip_hex) == 8 else _encode_v6mapped_hex(new_ip)
            return f"{new_hex}:{port}"
        if ip and ":" in ip:
            addr = ipaddress.IPv6Address(ip)
            key = addr.compressed
            if key in ip6_map:
                new_hex = _encode_v6_hex(ip6_map[key])
                return f"{new_hex}:{port}"
        return tok

    for i, line in enumerate(lines):
        m = NET_LINE_RE.match(line)
        if not m:
            continue
        prefix, local_tok, sep, remote_tok, rest = m.groups()
        local_tok = _rewrite_tok(local_tok)
        remote_tok = _rewrite_tok(remote_tok)
        lines[i] = prefix + local_tok + sep + remote_tok + rest
    return "\n".join(lines)


def _apply_literal_ips(text: str, ip_map: Dict[str, str]) -> str:
    def repl(m):
        if _is_version_context(text, m.start()):
            return m.group(0)
        ip = ".".join(m.groups())
        return ip_map.get(ip, ip)
    return IPV4_RE.sub(repl, text)


def _apply_literal_ipv6(text: str, ip6_map: Dict[str, str]) -> str:
    def repl(m):
        tok = m.group(0)
        if tok.count(":") < 2:
            return tok
        try:
            addr = ipaddress.IPv6Address(tok)
        except ValueError:
            return tok
        key = addr.compressed
        return ip6_map.get(key, tok)
    return IPV6_CANDIDATE_RE.sub(repl, text)


def _apply_macs(text: str, mac_map: Dict[str, str]) -> str:
    def repl(m):
        return mac_map.get(m.group(0).lower(), m.group(0))
    return MAC_RE.sub(repl, text)


def _apply_identifiers(text: str) -> str:
    text = SERIALNO_PROP_RE.sub(r"\1REDACTEDSERIAL", text)
    text = SERIALNO_KV_RE.sub(r"\1REDACTEDSERIAL", text)
    text = SERIAL_LABEL_RE.sub(r"\1REDACTEDSERIAL", text)
    text = SSID_RE.sub(r"\1REDACTED-SSID", text)
    text = ANDROID_ID_RE.sub(r"\1deadbeefdeadbeef", text)
    return text


def anonymize_text(
    text: str,
    ip_map: Dict[str, str],
    other_pkg_map: Dict[str, str],
    ip6_map: Dict[str, str] = None,
    mac_map: Dict[str, str] = None,
) -> str:
    ip6_map = ip6_map or {}
    mac_map = mac_map or {}
    text = _apply_pkg_maps(text, other_pkg_map)
    text = _apply_net_lines(text, ip_map, ip6_map)
    text = _apply_literal_ips(text, ip_map)
    text = _apply_literal_ipv6(text, ip6_map)
    text = _apply_macs(text, mac_map)
    text = _apply_identifiers(text)
    text = EMAIL_RE.sub("user@example.com", text)
    text = HOME_PATH_RE.sub(r"\1", text)
    return text


def find_violations(text: str) -> List[Tuple[str, str]]:
    violations: List[Tuple[str, str]] = []
    for needle in FORBIDDEN_SUBSTRINGS:
        if needle in text:
            violations.append(("forbidden_substring", needle))
    for m in PKG_RE.finditer(text):
        tok = m.group(0)
        if _is_pkg_allowlisted(tok):
            continue
        if tok.startswith("com.example."):
            continue
        if _is_reversed_domain_pkg(tok):
            violations.append(("package", tok))
    for m in IPV4_RE.finditer(text):
        if _is_version_context(text, m.start()):
            continue
        ip = ".".join(m.groups())
        if not _ip_allowed(ip):
            violations.append(("ipv4", ip))
    for m in IPV6_CANDIDATE_RE.finditer(text):
        tok = m.group(0)
        if tok.count(":") < 2:
            continue
        try:
            addr = ipaddress.IPv6Address(tok)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_unspecified or addr.ipv4_mapped is not None:
            continue
        if addr not in ANON_V6_NET:
            violations.append(("ipv6", tok))
    for line in text.splitlines():
        parsed = _net_line_tokens(line)
        if not parsed:
            continue
        _m, local_tok, remote_tok = parsed
        for tok in (local_tok, remote_tok):
            ip_hex, _, _port = tok.partition(":")
            if len(ip_hex) == 8:
                decoded = _decode_hex_addr_ip(ip_hex)
                if decoded and not _ip_allowed(decoded):
                    violations.append(("ipv4_hex", tok))
            elif len(ip_hex) == 32:
                decoded = _decode_hex_addr_ip(ip_hex)
                if decoded and ":" in decoded:
                    addr = ipaddress.IPv6Address(decoded)
                    if not (addr.is_loopback or addr.is_unspecified or addr.ipv4_mapped is not None):
                        if addr not in ANON_V6_NET:
                            violations.append(("ipv6_hex", tok))
    for m in MAC_RE.finditer(text):
        mac = m.group(0).lower()
        if mac != NULL_MAC and not mac.startswith("02:00:00"):
            violations.append(("mac", mac))
    for m in EMAIL_RE.finditer(text):
        if not m.group(0).lower().endswith("@example.com"):
            violations.append(("email", m.group(0)))
    return violations


def load_raw() -> Dict[str, str]:
    texts = {}
    for p in sorted(RAW_DIR.glob("*.txt")):
        if p.name in (SOURCES_FILE.name, PRIVATE_TERMS_FILE.name):
            continue
        texts[p.name] = p.read_text(encoding="utf-8", errors="replace")
    return texts


def anonymize_all() -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    raw = load_raw()
    all_texts = list(raw.values())
    ip_map = build_ip_mapping(all_texts)
    ip6_map = build_ip6_mapping(all_texts)
    mac_map = build_mac_mapping(all_texts)
    other_pkg_map = build_other_pkg_mapping(all_texts)
    out = {
        name: anonymize_text(text, ip_map, other_pkg_map, ip6_map, mac_map)
        for name, text in raw.items()
    }
    return out, ip_map, other_pkg_map


def _source_note() -> str:
    if SOURCES_FILE.exists():
        first = SOURCES_FILE.read_text(encoding="utf-8").strip().splitlines()
        if first:
            return first[0].strip()
    return "unknown"


def write_manifest(out: Dict[str, str]) -> None:
    """Escribe tests/fixtures/MANIFEST.txt: <name>\\t<sha256>\\t<line_count>\\t<source>.

    Generado siempre por este script a partir del contenido anonimizado real; no
    se edita a mano. La procedencia (<source>) se toma de la primera linea de
    tests/fixtures/raw/SOURCES.txt si existe (responsabilidad de quien captura
    los fixtures), o 'unknown' si no hay SOURCES.txt.
    """
    source = _source_note()
    kept: Dict[str, str] = {}
    if MANIFEST_FILE.exists():
        for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or "\t" not in line:
                continue
            name = line.split("\t", 1)[0]
            if name not in out and (OUT_DIR / name).exists():
                kept[name] = line
    lines = ["# name\tsha256\tlines\tsource"]
    for name in sorted(set(out) | set(kept)):
        if name in out:
            text = out[name]
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            line_count = len(text.splitlines())
            lines.append(f"{name}\t{digest}\t{line_count}\t{source}")
        else:
            lines.append(kept[name])
    MANIFEST_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    out, _ip_map, _pkg_map = anonymize_all()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in out.items():
        (OUT_DIR / name).write_text(text, encoding="utf-8")
    write_manifest(out)


if __name__ == "__main__":
    main()
