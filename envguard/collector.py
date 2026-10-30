"""Run every probe once and assemble the report.

Probes are independent: a failure in one becomes an `unknown` check rather than
aborting the run, because "the network lookup failed" is itself information the
user needs. The only ordering that matters is that the clock measurement also
supplies the Date header used nowhere else, so it runs after the egress lookups
(same connection pool warmth) and never gates them.
"""

import datetime as _dt

from . import checks, network, sysinfo, winnet

# Set by `--no-net`: skip the probes that leave the machine. The checks they feed
# then report `unknown`, which is excluded from the score rather than counted as
# a pass.
NO_NET = False


def collect(progress=None) -> checks.Report:
    """Gather every measurable fact and grade it.

    `progress(label)` is called before each step so a GUI can show movement
    during the network probes, which are the only slow part (~2-5 s).
    """

    def step(label: str) -> None:
        if progress is not None:
            progress(label)

    step("读取网卡与路由")
    adapters = winnet.list_adapters()
    default_if = winnet.default_route_interface()
    tunnels = [a for a in adapters if a.is_tunnel]
    vm_adapters = [a.name or a.description for a in adapters if a.is_virtual_machine]
    global_v6 = winnet.global_ipv6_addresses(adapters)

    step("读取系统代理")
    proxy = network.read_system_proxy()

    step("解析 DNS")
    dns_readings = network.read_dns_readings()
    dns_modes = {r.hostname: r.mode for r in dns_readings}

    step("查询出口 IP 与归属")
    if NO_NET:
        egress = network.EgressReading(source="none", ip=None, error="已按 --no-net 跳过联网探测")
        skew = None
    else:
        egress = network.egress_geo()
        step("测量时钟偏差")
        skew = sysinfo.measure_clock_skew()

    step("读取系统与客户端信息")
    os_reading = sysinfo.read_os()
    locale = sysinfo.read_locale(skew)
    claude = sysinfo.detect_claude_desktop()
    virtualization = sysinfo.read_virtualization()

    step("汇总")
    report = checks.build_report(
        region_country_code=egress.country_code,
        region_country_name=egress.country,
        org=egress.org,
        asn=egress.asn,
        egress_timezone=egress.timezone,
        system_offset_minutes=locale.timezone_offset_minutes,
        tunnel_adapters=[a.name or a.description for a in tunnels],
        tunnel_indexes={a.index for a in tunnels},
        default_if_index=default_if,
        registry_server=proxy.registry_server,
        env_http=proxy.env_http,
        proxy_same=proxy.same_target,
        listening_count=len(proxy.listening),
        dns_modes=dns_modes,
        global_v6=global_v6,
        egress_v4=egress.ip,
        clock_skew=skew,
        is_windows11=os_reading.is_windows11,
        os_product=os_reading.product_name,
        vm_services=virtualization.services,
        vm_adapters=vm_adapters,
        claude_installed=claude.installed,
        claude_running=claude.running,
        claude_version=claude.version,
        claude_data_dir=claude.data_dir_present,
        claude_store=claude.store_package,
    )

    report.generated_at = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    report.facts = {
        "egress": {
            "ip": egress.ip,
            "source": egress.source,
            "country": egress.country,
            "country_code": egress.country_code,
            "city": egress.city,
            "asn": egress.asn,
            "org": egress.org,
            "timezone": egress.timezone,
            "error": egress.error,
        },
        "adapters": [
            {
                "index": a.index,
                "name": a.name,
                "description": a.description,
                "tunnel": a.is_tunnel,
                "virtual_machine": a.is_virtual_machine,
                "addresses": list(a.addresses),
            }
            for a in adapters
        ],
        "default_route_if_index": default_if,
        "proxy": {
            "registry_enabled": proxy.registry_enabled,
            "registry_server": proxy.registry_server,
            "registry_override": proxy.registry_override,
            "autoconfig_url": proxy.autoconfig_url,
            "env_http": proxy.env_http,
            "env_https": proxy.env_https,
            "env_no_proxy": proxy.env_no_proxy,
            "listening": [f"{host}:{port}" for host, port in proxy.listening],
        },
        "dns": {r.hostname: {"mode": r.mode, "addresses": r.addresses} for r in dns_readings},
        "ipv6": global_v6,
        "os": {
            "product": os_reading.product_name,
            "build": os_reading.build,
            "display_version": os_reading.display_version,
            "architecture": os_reading.architecture,
            "is_windows11": os_reading.is_windows11,
        },
        "locale": {
            "system_locale": locale.system_locale,
            "ui_language": locale.ui_language,
            "timezone_id": locale.timezone_id,
            "utc_offset_minutes": locale.timezone_offset_minutes,
            "clock_skew_seconds": locale.clock_skew_seconds,
        },
        "claude": {
            "installed": claude.installed,
            "version": claude.version,
            "install_location": claude.install_location,
            "executable": claude.executable,
            "running": claude.running,
            "data_dir_present": claude.data_dir_present,
            "store_package": claude.store_package,
        },
        "virtualization": {
            "services": virtualization.services,
            "adapters": vm_adapters,
        },
    }
    return report
