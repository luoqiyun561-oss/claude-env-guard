# Claude 环境自检（ClaudeEnvGuard）

检测本机环境里**能被观测**的、与 Claude 风控相关的事实，给出一份带权重的报告。
只读：不写注册表、不改代理、不动浏览器数据、不登录任何账号。

## 产物

| 文件 | 用途 |
| --- | --- |
| `ClaudeEnvGuard.html` | **浏览器版**，双击即可，无需安装、无解压、无签名问题 |
| `dist/ClaudeEnvGuard-web.exe` | **推荐**：本地伴侣进程。伺服 HTML 并提供系统级数据，一个页面看全部 |
| `dist/ClaudeEnvGuard.exe` | 本机版图形界面（tkinter），单文件 10.7 MB |
| `dist/ClaudeEnvGuard-cli.exe` | 本机版命令行，用于脚本与计划任务（`--json` / `--md` / `--text`） |
| `dist/ClaudeEnvGuard-dir\` | 同上 GUI，**onedir 版**，从云同步目录运行请用这个 |
| `dist/ClaudeEnvGuard-cli-dir\` | 同上 CLI，onedir 版 |

**为什么 GUI 和 CLI 是两个**：`--windowed` 的 exe 没有 stdout，`--json` 打印不出来。

首次运行 exe 会弹 SmartScreen「Windows 已保护你的电脑」——这是没有代码签名的正常表现，
点「更多信息 → 仍要运行」。HTML 版没有这个问题。

## 推荐用法：web 伴侣进程

```
ClaudeEnvGuard-web.exe
```

双击后它会：后台跑一遍系统级检测 → 在 `http://127.0.0.1:8788/` 伺服页面并自动打开浏览器。
页面上同时呈现三块内容：

1. **浏览器侧检查**——WebRTC 泄漏、出口地区/类型、时区、语言（只有浏览器能测）
2. **系统侧检查**——TUN 接管路由、系统代理一致性、DNS 模式、时钟偏差、Claude 客户端（只有本机能读）
3. **自检列表**——机器测不到、只有用户知道的事，自己画勾

关闭那个控制台窗口即停止；不安装服务、不自启动、只绑定 127.0.0.1。
浏览器打开页面后，控制台窗口可以最小化，不要关（关了系统侧数据就没了，浏览器侧照常工作）。

**同源伺服是关键设计**：页面由伴侣进程自己提供，页面和系统数据在同一个 origin 上，
不存在 CORS 问题。直接双击 HTML 文件打开也能用，只是没有系统侧那一块（页面上有提示）。

## 自检列表说明

自检列表里的项目**不参与检测分**。原因写在页面上：这些是自报，不是测量；
把自报混进分数，分数就不再诚实。它只统计「已确认 x/y」，帮你别漏项。
勾选状态保存在浏览器 localStorage，随导出的 JSON 一起走。


## 两个版本测的东西不一样

| 检查项 | HTML 版 | exe 版 |
| --- | --- | --- |
| 出口地区 / IP 类型 / 时区 | ✅ | ✅ |
| **WebRTC 泄漏** | ✅（只有它能测） | ❌ |
| 浏览器语言一致性 | ✅ | ❌ |
| TUN 是否接管默认路由 | ❌ | ✅ |
| 系统代理 / 环境变量 / 端口一致性 | ❌ | ✅ |
| DNS 是否走 fake-IP | ❌ | ✅ |
| 系统时钟偏差 | ❌ | ✅ |
| Claude 桌面客户端 | ❌ | ✅ |
| 虚拟化环境 | ❌ | ✅ |

浏览器拿不到系统层的东西，系统工具拿不到 WebRTC。**两版互补**。

HTML 版里 WebRTC 拿不到候选时记「未检测」而不是「通过」——STUN 在国内经常不可达，
用测不到冒充没问题会误导判断。


### ⚠️ 单文件版不要放在云同步目录里（坚果云 / OneDrive 等）

单文件版运行时会先把自己解压到临时目录再启动。读到自己那一步需要文件在本地是**真实文件**。
云盘客户端会把它换成占位符（reparse point），单文件启动器读不动，于是弹出：

```
Could not create temporary directory!
```

**这和本程序无关，也不是权限问题。** 我用一个只有 `print("HELLO")` 的三行程序打成单文件，
放进坚果云目录同样报这个错；放到 `D:\` 就正常。

实测（2026-10-30，坚果云 reparse tag `0x9000601A`）：

| 位置 | 单文件版 | onedir 版 |
| --- | --- | --- |
| `D:\Software\ClaudeEnvGuard\` | 正常 | 正常 |
| 坚果云同步目录内 | 报错 | 正常 |

**两个解法，任选其一：**

1. **把单文件 exe 复制到非同步目录**（推荐）：
   ```powershell
   mkdir D:\Software\ClaudeEnvGuard
   copy dist\ClaudeEnvGuard.exe D:\Software\ClaudeEnvGuard\
   ```
2. **用 onedir 版**：整个 `ClaudeEnvGuard-dir` 文件夹留在同步目录即可，
   它不需要自解压，可以直接运行。

我已把单文件版复制到 `D:\Software\ClaudeEnvGuard\`，双击那里的即可。


## 命令行

```powershell
.\ClaudeEnvGuard-cli.exe              # 纯文本
.\ClaudeEnvGuard-cli.exe --json       # JSON（含全部原始数据）
.\ClaudeEnvGuard-cli.exe --md         # Markdown
.\ClaudeEnvGuard-cli.exe --no-net     # 跳过联网探测
```

## 检测项与权重

总分 = 各因素内加权平均。`未检测` 的项**不计入分母**，所以断网不会把分数拉低成"有风险"。

### 网络（120 分权重）

| 检查 | 权重 | 怎么判 |
| --- | --- | --- |
| 出口地区是否在支持范围 | 30 | 出口 IP 的国家码对比 Anthropic 公开的支持列表。中国大陆/香港/澳门单独点名 |
| 出口 IP 类型 | 25 | ASN 持有者名称判住宅/运营商 vs 机房云厂商 |
| TUN 是否接管默认路由 | 20 | `GetBestRoute` 取默认路由的 ifIndex，看它是不是隧道网卡 |
| 系统代理 / 环境变量 / 端口一致性 | 15 | WinINET 注册表 + `HTTP(S)_PROXY` + 本地端口 connect 探测 |
| DNS 解析模式 | 10 | 三个域名解析结果是否落在 fake-IP 段（从本地 Clash 配置读，不是硬编码） |
| 系统时区与出口地区是否一致 | 10 | 系统 UTC 偏移 vs 出口时区偏移 |
| 是否存在 IPv6 旁路 | 10 | 本机有无公网 IPv6 地址 |

### 设备（30 分权重）

| 检查 | 权重 | 怎么判 |
| --- | --- | --- |
| 操作系统类型 | 15 | Windows 与 macOS 在这件事上无差别，只用于确认不是 Linux |
| 系统时钟偏差 | 10 | 与三个端点的 `Date` 头比对，取中位数 |
| 虚拟化环境 | 5 | Hyper-V / WSL / VMware / VBox 的服务注册项 + 虚拟网卡 |

### 使用（20 分权重）

| 检查 | 权重 | 怎么判 |
| --- | --- | --- |
| Claude 桌面客户端 | 20 | 卸载注册表项或可执行文件；数据目录存在但没安装算 `注意` |

## 不检测的项目（刻意不做）

这些**无法在本机观测**，做成自评问卷会让报告看起来像测量结果，所以不做：

- 注册邮箱类型、注册时是否弹 KYC、是否用手机 App 注册
- 支付方式（实体卡 / Google Play / 虚拟卡）、订阅时机、Pro→Max 顺序
- 账号年龄、账号权重
- 记忆功能是否开启、记忆条数
- 服务端记录的多设备登录、登录地历史
- 是否大量使用 Fable
- Claude 内部的风险分本身

## 边界

这个工具做的是**环境自查**：帮你发现「自己的配置让流量从两个地方出去」这类可修的问题。
它不伪造设备指纹、不绕过 KYC、不自动注册或养号。

## 开发

```powershell
python -m envguard          # 直接跑 GUI
python -m envguard --text   # 直接跑 CLI
python build.py             # 重新打包两个 exe
```

代码结构：

- `envguard/winnet.py` — iphlpapi 直读网卡与路由（ctypes，无子进程）
- `envguard/network.py` — 系统代理、DNS 模式、出口 IP 与归属
- `envguard/sysinfo.py` — 系统版本、区域、时钟偏差、Claude 客户端、虚拟化
- `envguard/checks.py` — 权重与判定，纯函数，可单测
- `envguard/collector.py` — 组装
- `envguard/report.py` — JSON / Markdown / 文本
- `envguard/gui.py` — tkinter 界面
