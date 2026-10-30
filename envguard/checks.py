"""Turn raw readings into weighted checks with a verdict.

Design rules that make the score honest:

- **Four states, not two.** `good` / `warn` / `bad` / `unknown`. A check that
  could not run is `unknown` and is EXCLUDED from the denominator, so an offline
  machine does not silently score as a risky one.
- **Weight is declared, not tuned.** Every check names the factor it belongs to
  and why it has the weight it has. Nothing is a magic constant without a
  comment.
- **Only measured facts can lower the score.** The questionnaire items that
  cannot be observed locally (payment method, account age) are deliberately
  NOT part of this score; the tool would be lying if it folded a self-report
  into a number that reads as a measurement.

Region restriction is taken from Anthropic's published supported-region list.
When a country is unsupported, that single fact dominates — a clean IP inside an
unsupported region does not help, so it is reported as `bad` regardless of the
other network checks.
"""

from dataclasses import dataclass, field

# Anthropic's published list of countries/regions where Claude is available.
# Used only to answer "is this egress country served at all" — it is not a
# reputation model, and it is not a legal determination.
SUPPORTED_REGION_CODES = frozenset(
    """
    US CA GB IE FR DE NL BE LU CH AT IT ES PT DK SE NO FI IS PL CZ SK HU RO BG
    GR HR SI EE LV LT UA MD RS BA MK AL ME CY MT AD MC LI
    AU NZ JP KR SG HK TW IN ID MY PH TH VN BN KH LA MM MN
    IL AE SA QA KW BH OM JO LB TR GE AM AZ KZ UZ KG TJ TM
    ZA NG KE GH EG MA TN DZ CI SN TZ UG RW ET MU BW NA ZM ZW MZ AO CM
    BR MX AR CL CO PE UY PY BO EC VE CR PA GT HN SV NI DO JM TT BB BS BZ GY SR
    PR GU VI AS MP
    """.split()
)

# China mainland, Hong Kong, Macau are collected as their own fact so the report
# can name them instead of saying a vague "unsupported".
_CN_CODES = frozenset({"CN", "HK", "MO"})


@dataclass
class Check:
    id: str
    title: str
    factor: str            # network | device | behavior
    weight: int            # relative weight inside the factor
    status: str            # good | warn | bad | unknown
    detail: str
    advice: str | None = None

    @property
    def scored(self) -> bool:
        return self.status != "unknown"

    @property
    def ratio(self) -> float:
        return {"good": 1.0, "warn": 0.5, "bad": 0.0}.get(self.status, 0.0)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    generated_at: str = ""
    facts: dict = field(default_factory=dict)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def factor_score(self, factor: str) -> tuple[float, int, int]:
        """(0-100 score, earned weight, total weight) for one factor."""
        relevant = [c for c in self.checks if c.factor == factor and c.scored]
        total = sum(c.weight for c in relevant)
        if total == 0:
            return 0.0, 0, 0
        earned = sum(c.weight * c.ratio for c in relevant)
        return earned / total * 100, int(earned), total

    def total(self, factors: tuple[str, ...] = ("network", "device", "behavior")) -> tuple[float, int, int]:
        relevant = [c for c in self.checks if c.factor in factors and c.scored]
        total = sum(c.weight for c in relevant)
        if total == 0:
            return 0.0, 0, 0
        earned = sum(c.weight * c.ratio for c in relevant)
        return earned / total * 100, int(earned), total

    def verdict(self) -> str:
        score, _, _ = self.total()
        bad = sum(1 for c in self.checks if c.status == "bad")
        if bad >= 3 or score < 45:
            return "risky"
        if bad >= 1 or score < 75:
            return "caution"
        return "ok"


def region_check(country_code: str | None, country_name: str | None) -> Check:
    """Is the egress country one Claude serves at all?"""
    if not country_code:
        return Check(
            id="region",
            title="出口地区是否在支持范围内",
            factor="network",
            weight=30,
            status="unknown",
            detail="拿不到出口 IP 的国家信息",
            advice="检查代理是否连通后重跑",
        )
    code = country_code.upper()
    if code in SUPPORTED_REGION_CODES:
        return Check(
            id="region",
            title="出口地区是否在支持范围内",
            factor="network",
            weight=30,
            status="good",
            detail=f"{country_name or code}（{code}）在支持列表内",
        )
    if code in _CN_CODES:
        detail = f"出口在中国大陆/香港/澳门（{country_name or code}），Claude 不在这些地区提供服务"
    else:
        detail = f"出口在 {country_name or code}（{code}），不在 Claude 的支持列表内"
    return Check(
        id="region",
        title="出口地区是否在支持范围内",
        factor="network",
        weight=30,
        status="bad",
        detail=detail,
        advice="这不是「干净不干净」的问题，而是该地区根本没有这项服务。需要换成实际 Supported 地区的出口。",
    )


def ip_type_check(org: str | None, asn: str | None) -> Check:
    """Residential/mobile vs datacenter, inferred from the ASN holder name."""
    if not org and not asn:
        return Check(
            id="ip_type",
            title="出口 IP 类型（住宅/移动 vs 机房）",
            factor="network",
            weight=25,
            status="unknown",
            detail="没有拿到 ASN / 组织名",
        )
    haystack = f"{org or ''} {asn or ''}".lower()
    datacenter_markers = (
        "hosting",
        "cloud",
        "datacenter",
        "data center",
        "server",
        "vps",
        "amazon",
        "aws",
        "google llc",
        "microsoft",
        "azure",
        "digitalocean",
        "linode",
        "vultr",
        "ovh",
        "hetzner",
        "contabo",
        "oracle",
        "alibaba",
        "tencent cloud",
        "cloudflare",
    )
    mobile_markers = ("mobile", "wireless", "cellular", "4g", "5g", "lte", "unicom", "cmcc", "chinanet")
    if any(marker in haystack for marker in datacenter_markers):
        return Check(
            id="ip_type",
            title="出口 IP 类型（住宅/移动 vs 机房）",
            factor="network",
            weight=25,
            status="bad",
            detail=f"看起来是机房/云厂商 ASN：{org or asn}",
            advice="机房 IP 是最常见的扣分来源。换成住宅宽带或移动网络出口。",
        )
    if any(marker in haystack for marker in mobile_markers):
        return Check(
            id="ip_type",
            title="出口 IP 类型（住宅/移动 vs 机房）",
            factor="network",
            weight=25,
            status="good",
            detail=f"运营商网络：{org or asn}",
        )
    return Check(
        id="ip_type",
        title="出口 IP 类型（住宅/移动 vs 机房）",
        factor="network",
        weight=25,
        status="warn",
        detail=f"既不像机房也不明确像住宅：{org or asn}",
        advice="确认这条线路是目标国家的本地宽带，而不是小型 IDC 转售。",
    )


def timezone_match_check(
    timezone_id: str | None, offset_minutes: int | None, egress_timezone: str | None
) -> Check:
    """Does the machine's clock region match the exit IP's region?"""
    if not egress_timezone or offset_minutes is None:
        return Check(
            id="tz_match",
            title="系统时区与出口地区是否一致",
            factor="network",
            weight=10,
            status="unknown",
            detail="缺少出口时区或系统时区",
        )
    import datetime as _dt

    # Compare offsets rather than names: Windows reports "Pacific Standard Time"
    # where the lookup service says "America/Los_Angeles", and those name the
    # same zone. Only a real offset gap is worth reporting.
    egress_offset = _offset_for_iana(egress_timezone)
    if egress_offset is None:
        return Check(
            id="tz_match",
            title="系统时区与出口地区是否一致",
            factor="network",
            weight=10,
            status="unknown",
            detail=f"无法解析出口时区 {egress_timezone}",
        )
    gap = abs(egress_offset - offset_minutes)
    if gap <= 60:
        return Check(
            id="tz_match",
            title="系统时区与出口地区是否一致",
            factor="network",
            weight=10,
            status="good",
            detail=f"系统 UTC{_fmt(offset_minutes)} / 出口 {egress_timezone} UTC{_fmt(egress_offset)}",
        )
    if gap <= 240:
        return Check(
            id="tz_match",
            title="系统时区与出口地区是否一致",
            factor="network",
            weight=10,
            status="warn",
            detail=f"系统 UTC{_fmt(offset_minutes)} 与出口 {egress_timezone} UTC{_fmt(egress_offset)} 相差 {gap // 60} 小时",
            advice="按你列的规则，时区本身不是封号理由；但和别的信号叠加时它是一致性破绽，能对上更稳。",
        )
    return Check(
        id="tz_match",
        title="系统时区与出口地区是否一致",
        factor="network",
        weight=10,
        status="warn",
        detail=f"系统 UTC{_fmt(offset_minutes)} 与出口 {egress_timezone} UTC{_fmt(egress_offset)} 相差 {gap // 60} 小时",
        advice="差距这么大通常说明代理落地国家和你以为的不是同一个，先确认落地。",
    )


def _fmt(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    absolute = abs(minutes)
    return f"{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _offset_for_iana(name: str) -> int | None:
    """Current UTC offset in minutes for an IANA zone name.

    `zoneinfo` is deliberately not the only path: on Windows it needs the
    optional `tzdata` package, which a stock CPython install does not ship, and a
    frozen exe cannot assume it exists on the target machine either — measured
    here, ZoneInfo("Asia/Shanghai") raises ZoneInfoNotFoundError. The fallback
    table covers the zones a user of this tool realistically exits through, and
    `None` means "cannot say", which the caller reports as unknown rather than
    as a mismatch.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None
    if ZoneInfo is not None:
        import datetime as _dt

        try:
            delta = _dt.datetime.now(ZoneInfo(name)).utcoffset()
            if delta is not None:
                return int(round(delta.total_seconds() / 60))
        except Exception:
            pass
    return _IANA_OFFSET_MINUTES.get(name.strip())


# Standard-offset fallback. Fixed offsets ignore DST, so a DST zone can read one
# hour off — which is inside the tolerance the caller allows before warning.
_IANA_OFFSET_MINUTES = {
    "Asia/Shanghai": 480,
    "Asia/Hong_Kong": 480,
    "Asia/Macau": 480,
    "Asia/Taipei": 480,
    "Asia/Tokyo": 540,
    "Asia/Seoul": 540,
    "Asia/Singapore": 480,
    "Asia/Kuala_Lumpur": 480,
    "Asia/Bangkok": 420,
    "Asia/Jakarta": 420,
    "Asia/Manila": 480,
    "Asia/Kolkata": 330,
    "Asia/Dubai": 240,
    "Asia/Tel_Aviv": 120,
    "Europe/London": 0,
    "Europe/Dublin": 0,
    "Europe/Lisbon": 0,
    "Europe/Paris": 60,
    "Europe/Berlin": 60,
    "Europe/Madrid": 60,
    "Europe/Rome": 60,
    "Europe/Amsterdam": 60,
    "Europe/Brussels": 60,
    "Europe/Vienna": 60,
    "Europe/Zurich": 60,
    "Europe/Prague": 60,
    "Europe/Warsaw": 60,
    "Europe/Stockholm": 60,
    "Europe/Oslo": 60,
    "Europe/Copenhagen": 60,
    "Europe/Helsinki": 120,
    "Europe/Athens": 120,
    "Europe/Bucharest": 120,
    "Europe/Kyiv": 120,
    "Europe/Moscow": 180,
    "Europe/Istanbul": 180,
    "America/New_York": -300,
    "America/Toronto": -300,
    "America/Chicago": -360,
    "America/Denver": -420,
    "America/Phoenix": -420,
    "America/Los_Angeles": -480,
    "America/Vancouver": -480,
    "America/Mexico_City": -360,
    "America/Sao_Paulo": -180,
    "America/Argentina/Buenos_Aires": -180,
    "Australia/Sydney": 600,
    "Australia/Melbourne": 600,
    "Australia/Brisbane": 600,
    "Australia/Perth": 480,
    "Pacific/Auckland": 720,
    "UTC": 0,
    "Etc/UTC": 0,
}


def tun_check(tunnel_adapters: list[str], default_if_index: int | None, tunnel_indexes: set[int]) -> Check:
    """Is the default route actually carried by a tunnel adapter?"""
    if default_if_index is None:
        return Check(
            id="tun",
            title="TUN 是否真的接管了默认路由",
            factor="network",
            weight=20,
            status="unknown",
            detail="读不到默认路由",
        )
    if default_if_index in tunnel_indexes:
        names = "、".join(tunnel_adapters) or "隧道网卡"
        return Check(
            id="tun",
            title="TUN 是否真的接管了默认路由",
            factor="network",
            weight=20,
            status="good",
            detail=f"默认路由指向 {names}（ifIndex {default_if_index}）",
        )
    return Check(
        id="tun",
        title="TUN 是否真的接管了默认路由",
        factor="network",
        weight=20,
        status="bad",
        detail=f"默认路由走 ifIndex {default_if_index}，不是隧道网卡（找到的隧道：{'、'.join(tunnel_adapters) or '无'}）",
        advice="配置文件里的 tun.enable 不等于实际生效。以这里为准：默认路由必须在隧道网卡上。",
    )


def proxy_consistency_check(registry_server: str | None, env_http: str | None, same: bool | None, listening: int) -> Check:
    """Do the browser proxy, the environment proxy, and a live port agree?"""
    if registry_server is None and env_http is None:
        return Check(
            id="proxy_consistency",
            title="系统代理 / 环境变量 / 监听端口是否一致",
            factor="network",
            weight=15,
            status="bad",
            detail="既没有系统代理，也没有代理环境变量",
            advice="全局模式 + TUN 至少要有一个在生效，否则流量是直连的。",
        )
    if same is False:
        return Check(
            id="proxy_consistency",
            title="系统代理 / 环境变量 / 监听端口是否一致",
            factor="network",
            weight=15,
            status="warn",
            detail=f"系统代理 {registry_server or '未设'} 与环境变量 {env_http or '未设'} 指向不同地址",
            advice="两边指向不同代理时，浏览器和命令行会走不同出口，等于同时用了两个 IP。",
        )
    if listening == 0:
        return Check(
            id="proxy_consistency",
            title="系统代理 / 环境变量 / 监听端口是否一致",
            factor="network",
            weight=15,
            status="warn",
            detail=f"配置指向 {registry_server or env_http}，但常用代理端口都没有在监听",
            advice="配置存在但端口不通时，请求会失败而不是走代理，确认代理客户端在运行。",
        )
    return Check(
        id="proxy_consistency",
        title="系统代理 / 环境变量 / 监听端口是否一致",
        factor="network",
        weight=15,
        status="good",
        detail=f"系统代理与环境变量一致（{registry_server or env_http}），本地端口在监听",
    )


def dns_check(modes: dict[str, str]) -> Check:
    """What DNS mode is in force, and does it leak?"""
    if not modes:
        return Check(
            id="dns",
            title="DNS 解析模式",
            factor="network",
            weight=10,
            status="unknown",
            detail="没有可用的解析结果",
        )
    values = set(modes.values())
    if "private" in values:
        return Check(
            id="dns",
            title="DNS 解析模式",
            factor="network",
            weight=10,
            status="warn",
            detail=f"有域名解析到内网地址：{modes}",
            advice="这通常意味着 hosts 改写或本地 DNS 劫持，确认是不是你自己配置的。",
        )
    if values == {"fake-ip"}:
        return Check(
            id="dns",
            title="DNS 解析模式",
            factor="network",
            weight=10,
            status="good",
            detail="全部走 fake-IP（透明代理接管 DNS，符合全局 + TUN 的预期）",
        )
    if "unresolved" in values:
        return Check(
            id="dns",
            title="DNS 解析模式",
            factor="network",
            weight=10,
            status="warn",
            detail=f"有域名解析失败：{modes}",
            advice="解析失败会让客户端重试或回退，表现上像网络抖动；先确认 DNS 配置。",
        )
    return Check(
        id="dns",
        title="DNS 解析模式",
        factor="network",
        weight=10,
        status="warn",
        detail=f"返回真实公网地址而不是 fake-IP：{modes}",
        advice="真实解析意味着 DNS 查询可能没经过隧道，走的是本地运营商 DNS —— 这是一条常见的泄漏路径。",
    )


def ipv6_check(global_v6: list[str], egress_v4: str | None) -> Check:
    """A second, unprotected route out is worth knowing about."""
    if not global_v6:
        return Check(
            id="ipv6_leak",
            title="是否存在 IPv6 旁路",
            factor="network",
            weight=10,
            status="good",
            detail="没有分配公网 IPv6 地址，不存在 v6 旁路",
        )
    return Check(
        id="ipv6_leak",
        title="是否存在 IPv6 旁路",
        factor="network",
        weight=10,
        status="warn",
        detail=f"本机有公网 IPv6：{'、'.join(global_v6[:3])}"
        + (f"（v4 出口 {egress_v4}）" if egress_v4 else ""),
        advice="如果代理没有接管 v6，同一个站点会看到两个不同来源，等于在跳 IP。确认 v6 也在隧道内，或在网卡上关掉 v6。",
    )


def clock_check(skew_seconds: float | None) -> Check:
    if skew_seconds is None:
        return Check(
            id="clock",
            title="系统时钟偏差",
            factor="device",
            weight=10,
            status="unknown",
            detail="无法取到时间基准",
        )
    magnitude = abs(skew_seconds)
    if magnitude <= 5:
        return Check(
            id="clock",
            title="系统时钟偏差",
            factor="device",
            weight=10,
            status="good",
            detail=f"偏差 {skew_seconds:+.1f} 秒",
        )
    if magnitude <= 60:
        return Check(
            id="clock",
            title="系统时钟偏差",
            factor="device",
            weight=10,
            status="warn",
            detail=f"偏差 {skew_seconds:+.1f} 秒",
            advice="偏差过大会让 TLS 与 token 时间校验出错，开启自动同步时间。",
        )
    return Check(
        id="clock",
        title="系统时钟偏差",
        factor="device",
        weight=10,
        status="bad",
        detail=f"偏差 {skew_seconds:+.1f} 秒",
        advice="这么大的偏差会直接导致登录和请求失败，先校准系统时间。",
    )


def os_check(is_windows11: bool | None, product: str | None) -> Check:
    if is_windows11 is None:
        return Check(
            id="os_family",
            title="操作系统类型",
            factor="device",
            weight=15,
            status="unknown",
            detail="读不到系统版本",
        )
    return Check(
        id="os_family",
        title="操作系统类型",
        factor="device",
        weight=15,
        status="good",
        detail=f"{product or 'Windows'}（Windows 与 macOS 在这件事上没有区别）",
    )


def virtualization_check(services: dict[str, str], vm_adapters: list[str]) -> Check:
    """Virtualization is informational: it changes the network stack, not identity."""
    active = [name for name, state in services.items() if state in {"automatic", "boot", "system"}]
    if active or vm_adapters:
        parts = []
        if active:
            parts.append("服务：" + "、".join(active))
        if vm_adapters:
            parts.append("虚拟网卡：" + "、".join(vm_adapters))
        return Check(
            id="virtualization",
            title="虚拟化环境",
            factor="device",
            weight=5,
            status="warn",
            detail="；".join(parts),
            advice="虚拟化本身不是扣分项，但它会插进网络栈：确认代理没有把虚拟机网段当成需要绕过的局域网。",
        )
    return Check(
        id="virtualization",
        title="虚拟化环境",
        factor="device",
        weight=5,
        status="good",
        detail="未检测到常驻的虚拟化服务或虚拟网卡",
    )


def claude_client_check(
    installed: bool,
    running: bool,
    version: str | None,
    data_dir: bool,
    store_package: bool = False,
) -> Check:
    """Grade the desktop client, naming which install shape was found.

    The SKU is reported because it changes what the user should do next: a Store
    (MSIX) build updates itself and cannot be moved, while a classic install can.
    It also explains where the evidence came from — the Store build writes no
    Uninstall key, and that absence is what made an earlier version report
    "not installed" on a machine that had it.
    """
    if installed:
        kind = "（Microsoft Store 版）" if store_package else ""
        detail = f"已安装{kind}{(' 版本 ' + version) if version else ''}"
        detail += "，正在运行" if running else "，当前未运行"
        advice = None
        if not running:
            advice = "装好但没在运行。用它登录过一次，比只用网页端的权重更稳。"
        return Check(
            id="claude_client",
            title="Claude 桌面客户端",
            factor="behavior",
            weight=20,
            status="good",
            detail=detail,
            advice=advice,
        )
    if data_dir:
        return Check(
            id="claude_client",
            title="Claude 桌面客户端",
            factor="behavior",
            weight=20,
            status="warn",
            detail="找到 Claude 的数据目录，但没有可执行的安装记录",
            advice="目录残留说明装过又卸了。装回桌面端并按「一台机器一个客户端」的方式使用，比网页端更好。",
        )
    return Check(
        id="claude_client",
        title="Claude 桌面客户端",
        factor="behavior",
        weight=20,
        status="warn",
        detail="没有检测到 Claude 桌面客户端（查过卸载记录、常见安装路径与 Store 包）",
        advice="按你列的规则，桌面客户端有小幅加分、网页端没有。要用的话装上并稳定使用同一个客户端。",
    )


def build_report(
    *,
    region_country_code: str | None,
    region_country_name: str | None,
    org: str | None,
    asn: str | None,
    egress_timezone: str | None,
    system_offset_minutes: int | None,
    tunnel_adapters: list[str],
    tunnel_indexes: set[int],
    default_if_index: int | None,
    registry_server: str | None,
    env_http: str | None,
    proxy_same: bool | None,
    listening_count: int,
    dns_modes: dict[str, str],
    global_v6: list[str],
    egress_v4: str | None,
    clock_skew: float | None,
    is_windows11: bool | None,
    os_product: str | None,
    vm_services: dict[str, str],
    vm_adapters: list[str],
    claude_installed: bool,
    claude_running: bool,
    claude_version: str | None,
    claude_data_dir: bool,
    claude_store: bool = False,
) -> Report:
    report = Report()
    report.add(region_check(region_country_code, region_country_name))
    report.add(ip_type_check(org, asn))
    report.add(tun_check(tunnel_adapters, default_if_index, tunnel_indexes))
    report.add(proxy_consistency_check(registry_server, env_http, proxy_same, listening_count))
    report.add(dns_check(dns_modes))
    report.add(timezone_match_check(None, system_offset_minutes, egress_timezone))
    report.add(ipv6_check(global_v6, egress_v4))
    report.add(os_check(is_windows11, os_product))
    report.add(clock_check(clock_skew))
    report.add(virtualization_check(vm_services, vm_adapters))
    report.add(claude_client_check(claude_installed, claude_running, claude_version, claude_data_dir, claude_store))
    return report
