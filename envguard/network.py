"""Proxy, DNS and route facts.

Three questions, each answered from the place that actually decides it:

- **Is a system proxy set?** Read from the WinINET registry key that Windows
  itself consults. Not `Get-Item env:` — a proxy in the environment does not
  affect a browser, and a registry proxy does not affect a console tool. Both
  are collected because a mismatch between them is itself the finding.
- **Which proxy is *this* process actually using?** Answered by connecting and
  asking for our egress address, never by reading a config file. A Clash profile
  can say `mode: global` while the running core is in rule mode with a different
  config entirely — measured on this machine, where `config.yaml` says
  `tun.enable: false` but a `Meta Tunnel` adapter holds a default route.
- **Does DNS resolve through the tunnel?** A fake-IP DNS (Clash/mihomo default)
  answers public hostnames with reserved addresses. That is normal for these
  setups, so it is reported as a mode, not as a failure.
"""

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
import winreg
from dataclasses import dataclass, field

USER_AGENT = "ClaudeEnvGuard/1.0 (environment self-check)"

# Hosts whose answer shape tells us which DNS mode is in play.
_DNS_PROBES = ("api.anthropic.com", "claude.ai", "www.google.com")

# Fake-IP ranges a transparent proxy hands out for ordinary hostnames. Which
# range is in use is a per-profile setting, so local configs are read when
# present and these cover the common cores; measured on this machine with
# Clash Verge, whose profile sets `fake-ip-range: 28.0.0.1/8` — NOT the
# 198.18.0.0/15 that a hand-written checker usually assumes.
_FAKE_IP_DEFAULT_RANGES = (
    "198.18.0.0/15",
    "28.0.0.1/8",
    "fdfe:dcba:9876::/64",
    "fc00::/18",
)

_CLASH_CONFIG_PATHS = (
    r"%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml",
    r"%APPDATA%\clash-verge\config.yaml",
    r"%USERPROFILE%\.config\clash\config.yaml",
    r"%USERPROFILE%\.config\mihomo\config.yaml",
)


def _fake_ip_networks() -> tuple:
    """Ranges to treat as fake-IP: the defaults plus anything a local profile declares."""
    values = set(_FAKE_IP_DEFAULT_RANGES)
    for raw in _CLASH_CONFIG_PATHS:
        try:
            with open(os.path.expandvars(raw), encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            for key in ("fake-ip-range:", "fake-ip-range6:"):
                if stripped.startswith(key):
                    value = stripped[len(key) :].strip().strip("'\"")
                    if value:
                        values.add(value)
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return tuple(networks)


@dataclass
class ProxyReading:
    registry_enabled: bool | None
    registry_server: str | None
    registry_override: str | None
    autoconfig_url: str | None
    env_http: str | None
    env_https: str | None
    env_no_proxy: str | None
    listening: list[tuple[str, int]] = field(default_factory=list)

    @property
    def same_target(self) -> bool | None:
        """Do the registry proxy and the environment proxy name the same server?"""
        targets = {
            value
            for value in (self.registry_server, self.env_http, self.env_https)
            if value
        }
        normalized = {_normalize_proxy(value) for value in targets}
        return len(normalized) <= 1 if normalized else None


def _normalize_proxy(value: str) -> str:
    """`http://HOST:PORT` and `HOST:PORT` name the same proxy; compare one form."""
    text = value.strip().lower()
    for prefix in ("http://", "https://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.rstrip("/")


def read_system_proxy() -> ProxyReading:
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    enabled = server = override = autoconfig = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            def get(name):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return None

            raw_enabled = get("ProxyEnable")
            enabled = bool(raw_enabled) if raw_enabled is not None else None
            server = get("ProxyServer")
            override = get("ProxyOverride")
            autoconfig = get("AutoConfigURL")
    except OSError:
        pass

    return ProxyReading(
        registry_enabled=enabled,
        registry_server=server if isinstance(server, str) and server else None,
        registry_override=override if isinstance(override, str) else None,
        autoconfig_url=autoconfig if isinstance(autoconfig, str) and autoconfig else None,
        env_http=_clean_env("HTTP_PROXY") or _clean_env("http_proxy"),
        env_https=_clean_env("HTTPS_PROXY") or _clean_env("https_proxy"),
        env_no_proxy=_clean_env("NO_PROXY") or _clean_env("no_proxy"),
        listening=_listening_proxy_ports(),
    )


def _clean_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _listening_proxy_ports() -> list[tuple[str, int]]:
    """Loopback ports that answer like a local proxy (7890/7897/1080/...).

    A connect() probe instead of parsing netstat: the port list is short, the
    call is non-blocking, and "something is listening" is all this needs.
    """
    candidates = (7890, 7891, 7897, 7898, 7899, 1080, 1087, 8080, 9090, 9097)
    found = []
    for port in candidates:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                found.append(("127.0.0.1", port))
        except OSError:
            continue
    return found


@dataclass
class DnsReading:
    hostname: str
    addresses: list[str]
    mode: str  # "public" | "fake-ip" | "private" | "unresolved"


def resolve(hostname: str) -> DnsReading:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return DnsReading(hostname, [], "unresolved")
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        return DnsReading(hostname, [], "unresolved")
    # Check EVERY address against the fake-IP ranges, not just the IPv4 half: a
    # dual-stack profile hands out a fake IPv6 alongside a fake IPv4, and testing
    # only one family reports "public" for an answer no resolver produced.
    networks = _fake_ip_networks()

    def is_fake(value: str) -> bool:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(ip in network for network in networks if network.version == ip.version)

    if all(is_fake(a) for a in addresses):
        return DnsReading(hostname, addresses, "fake-ip")
    real = [a for a in addresses if not is_fake(a)]
    if any(_is_private(a) for a in real):
        return DnsReading(hostname, addresses, "private")
    return DnsReading(hostname, addresses, "public")


def _is_private(value: str) -> bool:
    """RFC1918 / ULA / link-local — a LAN answer rather than a public one."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def read_dns_readings() -> list[DnsReading]:
    return [resolve(host) for host in _DNS_PROBES]


@dataclass
class EgressReading:
    source: str
    ip: str | None
    country: str | None = None
    country_code: str | None = None
    city: str | None = None
    asn: str | None = None
    org: str | None = None
    timezone: str | None = None
    error: str | None = None


def _http_json(url: str, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _plain_text(url: str, timeout: float = 8.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace").strip()


def egress_ip() -> tuple[str | None, str | None]:
    """Our public IPv4 from the first endpoint that answers, plus that endpoint.

    Several are tried because a single service being down must not turn a
    checkable fact into an unknown one — measured on this machine, where
    api.ipify.org resets the TLS handshake while ipv4.icanhazip.com answers.
    """
    for name, fetch in (
        ("ipv4.icanhazip.com", lambda: _plain_text("https://ipv4.icanhazip.com")),
        ("api.ipify.org", lambda: _http_json("https://api.ipify.org?format=json")["ip"]),
        ("cloudflare/cdn-cgi/trace", lambda: _trace_ip()),
    ):
        try:
            value = fetch().strip()
            ipaddress.ip_address(value)
            return value, name
        except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError):
            continue
    return None, None


def _trace_ip() -> str:
    text = _plain_text("https://www.cloudflare.com/cdn-cgi/trace")
    for line in text.splitlines():
        if line.startswith("ip="):
            return line[3:]
    raise ValueError("no ip= line in trace output")


def egress_geo() -> EgressReading:
    """Country / ASN / org for our egress, from whichever lookup answers."""
    ip, source = egress_ip()
    if ip is None:
        return EgressReading(source="none", ip=None, error="所有 IP 回显端点都不可达")

    # ipwho.is carries country + ASN + ISP in one call and needs no key.
    try:
        data = _http_json("https://ipwho.is/")
        if data.get("success"):
            connection = data.get("connection") or {}
            return EgressReading(
                source="ipwho.is",
                ip=ip,
                country=data.get("country"),
                country_code=data.get("country_code"),
                city=data.get("city"),
                asn=str(connection.get("asn")) if connection.get("asn") else None,
                org=connection.get("isp") or connection.get("org"),
                timezone=(data.get("timezone") or {}).get("id"),
            )
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass

    try:
        data = _http_json("https://api.ipapi.is/")
        return EgressReading(
            source="api.ipapi.is",
            ip=data.get("ip") or ip,
            country=data.get("country"),
            country_code=data.get("country_code"),
            city=data.get("city"),
            asn=data.get("asn"),
            org=data.get("company"),
            timezone=data.get("timezone"),
        )
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass

    return EgressReading(source=source or "none", ip=ip, error="拿到了出口 IP，但地理位置查询全部失败")
