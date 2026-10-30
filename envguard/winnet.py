"""Windows network facts read straight from iphlpapi.

Why ctypes and not `ipconfig` / `Get-NetAdapter`: this ships as a single-file exe,
so every subprocess call is both slow (400ms+) and a new opportunity to fail on a
machine whose PowerShell profile or PATH is unusual. iphlpapi answers the two
questions this tool actually has — "which adapters exist" and "which one carries
the default route" — in a few milliseconds with no child process.

Read-only: nothing here writes a registry key, an adapter, or a route.
"""

import ctypes
import ipaddress
import socket
from ctypes import POINTER, Structure, byref, c_ulong, c_void_p, cast
from dataclasses import dataclass

# Adapter description keywords that identify a tunnel/VPN adapter. Matched
# case-insensitively against name+description, because a user can rename the
# adapter but Device Manager still reports the vendor string.
TUNNEL_HINTS = (
    "tun",
    "tap",
    "wintun",
    "wireguard",
    "tailscale",
    "zerotier",
    "openvpn",
    "meta tunnel",
    "clash",
    "sing-box",
    "v2ray",
    "mihomo",
    "surge",
    "stash",
)

VM_HINTS = (
    "vmware",
    "virtualbox",
    "hyper-v",
    "vethernet",
    "qemu",
    "parallels",
    "virtual machine",
)

# Address ranges that are not a real egress address.
_NON_ROUTABLE = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


class SOCKET_ADDRESS(Structure):
    _fields_ = [("lpSockaddr", c_void_p), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(Structure):
    pass


# The head of IP_ADAPTER_UNICAST_ADDRESS: a linked-list entry plus the sockaddr.
# `Next` must be declared or walking the list reads the wrong offset.
IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", c_ulong),
    ("Flags", c_ulong),
    ("Next", POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
]


class IP_ADAPTER_ADDRESSES(Structure):
    pass


# The real struct opens with `union { ULONGLONG Alignment; struct { ULONG Length;
# IF_INDEX IfIndex; }; }`, so `Next` starts at offset 8 on x64. The union must
# contain a STRUCT holding both 32-bit fields — putting Length and IfIndex as two
# members of the union itself makes them overlap at offset 0, which silently
# reports Length (the struct size) as every adapter's index.
#
# `AdapterName` is a PCHAR (pointer to an ANSI name), NOT an inline char[260];
# declaring it inline shifts every following pointer by 260 bytes and is why a
# naive port reads empty friendly names and null address lists.
class _LENGTH_IFINDEX(Structure):
    _fields_ = [("Length", c_ulong), ("IfIndex", c_ulong)]


class _ALIGNMENT_UNION(ctypes.Union):
    _fields_ = [("Alignment", ctypes.c_ulonglong), ("parts", _LENGTH_IFINDEX)]


IP_ADAPTER_ADDRESSES._fields_ = [
    ("u", _ALIGNMENT_UNION),
    ("Next", POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", c_void_p),
    ("FirstUnicastAddress", POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", c_void_p),
    ("FirstMulticastAddress", c_void_p),
    ("FirstDnsServerAddress", c_void_p),
    ("DnsSuffix", c_void_p),
    ("Description", c_void_p),
    ("FriendlyName", c_void_p),
]


class MIB_IPFORWARDROW(Structure):
    _fields_ = [
        ("dwForwardDest", c_ulong),
        ("dwForwardMask", c_ulong),
        ("dwForwardPolicy", c_ulong),
        ("dwForwardNextHop", c_ulong),
        ("dwForwardIfIndex", c_ulong),
        ("dwForwardType", c_ulong),
        ("dwForwardProto", c_ulong),
        ("dwForwardAge", c_ulong),
        ("dwForwardNextHopAS", c_ulong),
        ("dwForwardMetric1", c_ulong),
        ("dwForwardMetric2", c_ulong),
        ("dwForwardMetric3", c_ulong),
        ("dwForwardMetric4", c_ulong),
        ("dwForwardMetric5", c_ulong),
    ]


@dataclass(frozen=True)
class Adapter:
    index: int
    name: str
    description: str
    addresses: tuple[str, ...]

    @property
    def is_tunnel(self) -> bool:
        text = f"{self.name} {self.description}".lower()
        return any(hint in text for hint in TUNNEL_HINTS)

    @property
    def is_virtual_machine(self) -> bool:
        text = f"{self.name} {self.description}".lower()
        return any(hint in text for hint in VM_HINTS)


def _sockaddr_text(sockaddr_ptr: int | None) -> str | None:
    """Decode an AF_INET / AF_INET6 sockaddr into its bare address text."""
    if not sockaddr_ptr:
        return None
    family = ctypes.cast(sockaddr_ptr, POINTER(ctypes.c_ushort))[0]
    try:
        if family == socket.AF_INET:
            return str(ipaddress.IPv4Address(ctypes.string_at(sockaddr_ptr + 4, 4)))
        if family == socket.AF_INET6:
            return str(ipaddress.IPv6Address(ctypes.string_at(sockaddr_ptr + 8, 16)))
    except (ValueError, OSError):
        return None
    return None


def _unicast_addresses(head: POINTER(IP_ADAPTER_UNICAST_ADDRESS)) -> tuple[str, ...]:
    found = []
    node = head
    # Bounded so a malformed list can never spin forever.
    for _ in range(256):
        if not node:
            break
        text = _sockaddr_text(node.contents.Address.lpSockaddr)
        if text is not None:
            found.append(text)
        node = node.contents.Next
    return tuple(found)


def list_adapters() -> list[Adapter]:
    """Every IPv4/IPv6 adapter, including ones with no address assigned."""
    iphlpapi = ctypes.WinDLL("iphlpapi.dll")
    size = c_ulong(16_000)
    buffer = None
    for _ in range(4):
        buffer = ctypes.create_string_buffer(size.value)
        # GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST: unicast + DNS only.
        result = iphlpapi.GetAdaptersAddresses(
            c_ulong(socket.AF_INET), c_ulong(0x0002 | 0x0004), None, buffer, byref(size)
        )
        if result == 0:
            break
        if result != 111:  # ERROR_BUFFER_OVERFLOW retries with the reported size
            return []
    else:
        return []

    adapters: list[Adapter] = []
    node = cast(buffer, POINTER(IP_ADAPTER_ADDRESSES))
    for _ in range(256):
        if not node:
            break
        entry = node.contents
        adapters.append(
            Adapter(
                index=int(entry.u.parts.IfIndex),
                name=ctypes.wstring_at(entry.FriendlyName) if entry.FriendlyName else "",
                description=ctypes.wstring_at(entry.Description) if entry.Description else "",
                addresses=_unicast_addresses(entry.FirstUnicastAddress),
            )
        )
        node = entry.Next
    return adapters


def default_route_interface() -> int | None:
    """Interface index carrying 0.0.0.0/0, or None when the lookup fails."""
    iphlpapi = ctypes.WinDLL("iphlpapi.dll")
    row = MIB_IPFORWARDROW()
    result = iphlpapi.GetBestRoute(c_ulong(0), c_ulong(0), byref(row))
    if result != 0:
        return None
    return int(row.dwForwardIfIndex)


def is_routable(address: str) -> bool:
    """False for loopback, link-local and RFC1918-style addresses."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not any(ip in network for network in _NON_ROUTABLE)


def global_ipv6_addresses(adapters: list[Adapter] | None = None) -> list[str]:
    """Routable IPv6 addresses currently assigned to this machine."""
    found = []
    for adapter in adapters if adapters is not None else list_adapters():
        for address in adapter.addresses:
            if ":" in address and is_routable(address):
                found.append(address)
    return found
