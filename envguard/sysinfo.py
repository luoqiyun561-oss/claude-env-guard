"""OS, locale, clock and Claude-client facts.

Every value here comes from a Windows API, a registry key, or a file the app
itself wrote. Nothing is inferred from a filename guess: "a Claude folder
exists" and "Claude Desktop is installed" are different claims, and only the
second one is a fact this tool reports (an uninstall entry or the executable).
"""

import ctypes
import datetime as _dt
import json
import os
import subprocess
import winreg
from dataclasses import dataclass, field

_UNINSTALL_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
)


@dataclass
class OsReading:
    product_name: str | None
    build: str | None
    display_version: str | None
    architecture: str
    is_windows11: bool | None


def read_os() -> OsReading:
    def query(root, path, name):
        try:
            with winreg.OpenKey(root, path) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    path = r"Software\Microsoft\Windows NT\CurrentVersion"
    product = query(winreg.HKEY_LOCAL_MACHINE, path, "ProductName")
    build_text = query(winreg.HKEY_LOCAL_MACHINE, path, "CurrentBuild")
    display = query(winreg.HKEY_LOCAL_MACHINE, path, "DisplayVersion")
    build_number = None
    if isinstance(build_text, str):
        try:
            build_number = int(build_text)
        except ValueError:
            build_number = None
    return OsReading(
        product_name=product if isinstance(product, str) else None,
        build=build_text if isinstance(build_text, str) else None,
        display_version=display if isinstance(display, str) else None,
        architecture=os.environ.get("PROCESSOR_ARCHITECTURE", "unknown"),
        # Windows 11 starts at build 22000; this is how the OS reports itself,
        # which is more reliable than ProductName on upgraded installs.
        is_windows11=(build_number >= 22000) if build_number is not None else None,
    )


@dataclass
class LocaleReading:
    system_locale: str
    ui_language: str | None
    timezone_id: str
    timezone_offset_minutes: int
    clock_skew_seconds: float | None
    machine_utc_offset_minutes: int


def read_locale(clock_skew_seconds: float | None) -> LocaleReading:
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_unicode_buffer(85)
    language = None
    if kernel32.GetUserDefaultLocaleName(buffer, 85):
        language = buffer.value

    now = _dt.datetime.now()
    # Minutes AHEAD of UTC, the sign convention every other tool reports:
    # UTC+08:00 is +480, not -480.
    delta = now.astimezone().utcoffset()
    offset = int(round(delta.total_seconds() / 60)) if delta else 0
    return LocaleReading(
        system_locale=_windows_locale_name(),
        ui_language=language,
        timezone_id=_timezone_id(),
        timezone_offset_minutes=offset,
        clock_skew_seconds=clock_skew_seconds,
        machine_utc_offset_minutes=offset,
    )


def _windows_locale_name() -> str:
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_unicode_buffer(85)
    if kernel32.GetSystemDefaultLocaleName(buffer, 85):
        return buffer.value
    return "unknown"


def _timezone_id() -> str:
    kernel32 = ctypes.windll.kernel32
    # TIME_ZONE_ID_* values are enough to name the shape; the IANA id is read
    # from the registry so the report shows the same string the OS control panel
    # does rather than a translated display name.
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
        ) as key:
            value = winreg.QueryValueEx(key, "TimeZoneKeyName")[0]
            return str(value)
    except OSError:
        pass
    return "unknown"


def measure_clock_skew(timeout: float = 6.0) -> float | None:
    """Seconds between this machine's clock and an HTTP Date header.

    A large skew breaks TLS validation and OAuth token lifetimes, so it is a
    real blocker rather than a curiosity. Three endpoints are tried because the
    measurement is worthless if the only source is a captive-portal page.
    """
    import email.utils
    import urllib.request

    samples = []
    for url in (
        "https://www.cloudflare.com/cdn-cgi/trace",
        "https://ipv4.icanhazip.com",
        "https://api.ipapi.is/",
    ):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ClaudeEnvGuard/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                header = response.headers.get("Date")
                if not header:
                    continue
                parsed = email.utils.parsedate_to_datetime(header)
                if parsed.tzinfo is None:
                    continue
                samples.append((parsed - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
        except Exception:
            continue
    if not samples:
        return None
    samples.sort()
    return round(samples[len(samples) // 2], 1)


@dataclass
class ClaudeClient:
    installed: bool
    display_name: str | None = None
    version: str | None = None
    install_location: str | None = None
    executable: str | None = None
    running: bool = False
    data_dir_present: bool = False
    store_package: bool = False


def detect_claude_desktop() -> ClaudeClient:
    """Find a real Claude Desktop install and say whether it is running.

    THREE install shapes, because the vendor ships all three and a checker that
    knows only one reports a false negative on the others:

      1. classic installer — an "Uninstall" registry entry with an install path;
      2. portable/3p builds — a Claude.exe under LocalAppData or Program Files;
      3. MSIX / Microsoft Store — a package under `Program Files\\WindowsApps`.

    Shape 3 is why this function grew: the Store build writes NO Uninstall key at
    all, so the previous version reported "not installed" on a machine whose Start
    menu visibly listed Claude (measured 2026-10-31: package
    `Claude_4.4212.0.0_x64__pzs8sxrjxfjjc`). A stray data folder is still only
    `data_dir_present`, never proof of installation, because it survives an
    uninstall.
    """
    display = version = location = None
    for root, path in _UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(root, path) as parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    try:
                        name = winreg.EnumKey(parent, index)
                        with winreg.OpenKey(parent, name) as entry:
                            def value(key):
                                try:
                                    return winreg.QueryValueEx(entry, key)[0]
                                except OSError:
                                    return None

                            label = value("DisplayName") or ""
                            if "claude" not in str(label).lower():
                                continue
                            display = str(label)
                            version = str(value("DisplayVersion") or "") or None
                            location = str(value("InstallLocation") or "") or None
                    except OSError:
                        continue
        except OSError:
            continue
        if display:
            break

    executable = None
    for candidate in (
        os.path.expandvars(r"%LOCALAPPDATA%\AnthropicClaude\claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Claude\Claude.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Claude\Claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Claude-3p\Claude.exe"),
    ):
        if os.path.isfile(candidate):
            executable = candidate
            break

    # MSIX / Store build: no Uninstall key, and the real app sits under the
    # access-restricted WindowsApps folder so a plain os.path.isfile probe can
    # fail even after os.path.isdir succeeds. The package *directory name* is the
    # reliable signal, and it carries the version.
    store_package = None
    if display is None:
        store_package = _store_claude_package()
        if store_package is not None:
            display = "Claude（Microsoft Store 版）"
            version = store_package["version"] or version
            location = store_package["path"]
            executable = store_package["executable"]

    data_dir = os.path.expandvars(r"%APPDATA%\Claude")
    return ClaudeClient(
        installed=bool(display) or executable is not None,
        display_name=display,
        version=version,
        install_location=location,
        executable=executable,
        running=_process_running(("claude.exe",)),
        data_dir_present=os.path.isdir(data_dir),
        store_package=store_package is not None,
    )


def _store_claude_package() -> dict | None:
    """Locate an MSIX-installed Claude.

    The obvious probe — `os.listdir(%PROGRAMFILES%\\WindowsApps)` — is refused
    with access denied even for an administrator, because that folder carries a
    service-owned ACL (measured 2026-10-31). The package repository under HKCU is
    readable without elevation and lists the same package identity, so enumeration
    goes through the registry; only the final existence check touches the
    filesystem, and a KNOWN path under WindowsApps is readable even though the
    parent directory listing is not.

    Package ids look like `Claude_4.4212.0.0_x64__pzs8sxrjxfjjc`; the second
    underscore-separated field is the version.
    """
    repo = (
        r"Software\Classes\Local Settings\Software\Microsoft\Windows"
        r"\CurrentVersion\AppModel\Repository\Packages"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, repo) as parent:
            count = winreg.QueryInfoKey(parent)[0]
            for index in range(count):
                try:
                    name = winreg.EnumKey(parent, index)
                except OSError:
                    continue
                if not name.lower().startswith("claude_"):
                    continue
                parts = name.split("_")
                version = parts[1] if len(parts) > 1 else None
                package_dir = os.path.join(
                    os.path.expandvars(r"%PROGRAMFILES%\WindowsApps"), name
                )
                exe = None
                for candidate in (
                    os.path.join(package_dir, "app", "Claude.exe"),
                    os.path.join(package_dir, "Claude.exe"),
                ):
                    if os.path.isfile(candidate):
                        exe = candidate
                        break
                return {
                    "name": name,
                    "version": version,
                    "path": package_dir,
                    "executable": exe,
                }
    except OSError:
        return None
    return None


def _process_running(names: tuple[str, ...]) -> bool:
    """Whether one of `names` is running, via tasklist.

    ctypes enumeration would avoid the subprocess, but the process list is one
    of the few places where the shell tool is both faster than a
    Toolhelp32 snapshot loop and already guaranteed present on every Windows
    SKU. A failure here answers False, which the report shows as "not running"
    rather than as a broken check.
    """
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    text = completed.stdout.decode("mbcs", "replace").lower()
    return any(name.lower() in text for name in names)


@dataclass
class VirtualizationReading:
    services: dict[str, str] = field(default_factory=dict)
    vm_adapters: list[str] = field(default_factory=list)


_VM_SERVICES = (
    "vmms",        # Hyper-V
    "vmcompute",   # Hyper-V Host Compute
    "LxssManager", # WSL
    "VBoxSup",     # VirtualBox support driver
    "VMwareHostd", # VMware Workstation
)


def read_virtualization() -> VirtualizationReading:
    services = {}
    for name in _VM_SERVICES:
        state = _service_state(name)
        if state is not None:
            services[name] = state
    return VirtualizationReading(services=services)


def _service_state(name: str) -> str | None:
    """Service state, or None when the service does not exist."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}"
        ) as key:
            try:
                start = winreg.QueryValueEx(key, "Start")[0]
            except OSError:
                start = None
    except OSError:
        return None
    # Start: 0 boot, 1 system, 2 auto, 3 manual, 4 disabled.
    return {0: "boot", 1: "system", 2: "automatic", 3: "manual", 4: "disabled"}.get(
        start, "present"
    )


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
