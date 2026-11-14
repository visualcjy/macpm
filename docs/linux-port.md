# macpm → Linux（x86_64 / arm64）移植可行性评估

评估对象：macpm v3.0（本仓库，约 4.3k 行核心代码 + 1.1k 行测试）。

> **实现状态（2026-11-14）：已落地。** 平台抽象层（`macpm/backend.py`）、Linux 后端
> （`macpm/linux_backend/`）与 UI 能力适配已完成，`python -m unittest discover -s tests`
> 全绿（157 个用例，其中约 60 个覆盖 Linux 后端）。与本文档评估的差异：
>
> | 评估项 | 实际实现 |
> |---|---|
> | CPU 簇/频率 | ✅ `topology/core_type` → `cpu_capacity` → `cpuinfo_max_freq` 三级探测；有 `time_in_state` 时按 residency 加权 |
> | CPU 功率 | ✅ RAPL（优先 core 子域，避免与核显重复计数），root 或 udev 授权 |
> | GPU 利用率/频率/功耗 | ✅ amdgpu / i915+xe（fdinfo 引擎时间聚合）/ nvidia-smi |
> | 温度/风扇 | ✅ hwmon，按 chip name + label 分类 CPU/GPU |
> | 进程 GPU% / 进程表 | ✅ DRM `drm-engine-*` fdinfo（非 root 仅本用户）；厂商卡走 `pmon`/`--query-compute-apps`，按 PID 合并卡号与显存，无 SM% 的厂商显示 `—` |
> | 网络视图 | ✅ `/proc/net/*` + `ss -tanie`（TCP only），监听表按 inode 归属 |
> | GPU 视图（多卡） | ✅ key `5`：进程行右侧显示模型名（沿父进程链读 `vllm serve`/`sglang.launch_server` 的 cmdline，不读模型目录）；顶部 GPU INFO（卡名/设备数/卡数/dies per card/驱动/CUDA/PCIe/VBIOS；teco 不推断卡数），单表每卡两行、跨卡进程在每张卡下各列一次（带 `[8-9]` 范围）（busy/时钟/功耗/VRAM 进度条/温度 + 该卡进程，缩进 `└`），`↑/↓` 整页翻页、页脚 `Page N/M`；单卡保留两栏布局 |
> | ANE | ❌ 按设计删除（Linux 无对应计数器） |
> | 独立 GPU 视图（key `5`） | ✅ `macpm/gpuview.py`：busy/时钟/功耗/VRAM/温度 + 按 GPU 时间排序的进程表 |
>
> **真机验证（2026-11-14，LoongArch64 集群）**：
> - 天数 BI-V150S 节点（Loongnix 23.1 / 龙芯 3C6000 / 128 核 / 16×BI-V150S）：容器内 `macpm.probe`
>   与 `macpm.dump` 正常，CPU/内存/磁盘/网络/温度可用；无 cpufreq、无 RAPL → 频率/功耗 n/a；
>   16 张卡经新增的 `ixsmi` 后端全部列出（busy/500–1750MHz/12–22W/29G·32G/28–31°C，合计 207W）。
>   `ixsmi pmon` 在该驱动上不报 SM%，因此进程 GPU 表为空。
> - 登临节点（同类 LoongArch，64×KS58 co-processor、`/dev/dlgpu*`、SMI 工具为 `/usr/bin/dlsmi`）：
>   新增 `dlsmi` 后端后按卡枚举；用真实 64 行输出验证解析：64 张卡、功率合计 378.71W、
>   显存 366/1024 GiB、温度 36–59°C、时钟 1000/1000MHz。`dlsmi` 的
>   `--query-compute-apps=pid,process_name,used_memory,gpu_index` 提供 PID/进程/显存/卡号，
>   进程表按 PID 合并（sglang 的 25 个进程、卡号 0–23）。
> - 太初 gpu020/gpu019（经管理服务器37跳板，同类 LoongArch）：`teco-smi` 位于
>   `/opt/tecoai/bin`，8×TECO_AICARD_01；新增 `teco` 后端后在 gpu020 上实测 8 卡全部列出
>   （0%、2360/3000MHz、88–101.5W、60737/65536MB、37–43°C），工具无 per-process 利用率。
> - 三个节点均无 DRM 计算卡（只有 ASPEED BMC），因此不依赖 DRM 的厂商后端是必需的。
>
> 复现方式（无需 Linux 机器）：`tests/linux_fixtures.py` 生成 hybrid x86 / arm64
> big.LITTLE / 空机三套 `/proc`+`/sys` 夹具，配 `MACPM_SYSROOT` 与
> `MACPM_PLATFORM=linux` 可在 macOS 上跑完整 Linux 采集与无头渲染。

目标平台：x86_64 Linux 与 arm64 Linux（含 Asahi/ARM 笔记本、树莓派类 SBC、Graviton/Ampere 类服务器）。


### 实现结构

```
macpm/
  backend.py              # 平台选择：create_sampler / create_network_sampler /
                          # process_gpu_reader / default_capabilities
  capabilities.py         # Capabilities 数据类（UI 据此隐藏或显示 n/a）
  metrics.py              # 与平台无关的计算（calc_freq、能量合理性检查）
  netmodel.py             # 网络视图数据模型与格式化（两平台共用）
  macos_gpu.py            # IOAccelerator per-process GPU（惰性 dlopen）
  gpuview.py              # GPU 视图（key 5，两平台共用）
  linux_backend/
    sysfs.py              # SysRoot（支持 MACPM_SYSROOT 回放）
    cpu.py                # 簇探测、per-core 利用率/频率、time_in_state
    soc.py                # CPU/板卡型号、RAPL 限制功耗、显示量纲
    power.py              # RAPL / hwmon / 电池
    thermal.py            # hwmon 温风扇分类、PSI/节流计数
    gpu.py                # amdgpu / i915 / xe / nvidia + fdinfo 聚合
    proc.py               # DRM fdinfo 扫描、socket inode → pid
    net.py                # /proc/net/* + ss -tanie
    sampler.py            # powermetrics 形状的 Linux 采样器
```

UI 侧改动集中在 `macpm/macpm.py`：`DefaultView` 按 `Capabilities` 决定是否创建
E 簇 gauge、GPU/ANE gauge 和 Power 面板，`get_process_summary` 按平台给出内存细分行。

---

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| 能不能移植？ | **能。** UI 层（dashing + blessed + curses）与 macOS 无关，可以直接跑在 Linux 上；约一半代码可原样复用。 |
| 移植后还是同一个产品吗？ | **不再是"rootless 全功能"。** Linux 缺少 IOReport / AppleSMC / nettop 这条"无 root 拿到硬件计数器"的通道，功率、GPU 利用率、跨用户进程数据基本都要降级或依赖 root/厂商驱动。 |
| x86_64 能覆盖多少？ | 功能面 **约 75%**：CPU/内存/磁盘/网络/进程/温度/风扇 + RAPL 功率（需 root 或 udev 授权）+ 厂商 GPU 后端。 |
| arm64 能覆盖多少？ | **约 50–55%**：CPU/内存/磁盘/网络/进程可用；温度/风扇取决于 hwmon 驱动；功率与 GPU 利用率大多不可用；ANE 永远没有。 |
| 哪些功能永久拿不到？ | ANE 占用/功率、跨用户 per-process GPU%（无 root）、跨用户 per-process 网络流量（无 root）、Apple 语义的 P-state residency 平均频率。 |
| 建议 | 不要 fork 成独立项目，而是在同仓库加**平台抽象层 + Linux 后端**；Power/ANE/GPU% 面板在 Linux 上按能力自动隐藏或显示 `n/a`。 |

**一句话判断**：可移植性"高"，但价值会从"macOS 上 powermetrics 的免费替代"变成"btop/nvtop 的又一个同类实现"，因为 Linux 上 btop、nvtop、bottom 已经把能拿到的数据都拿过了。真正值得移植的是 macpm 的**界面与视图组织**，不是它的采集层。

---

## 2. 现在的代码为什么在 Linux 上根本起不来

三个模块在**导入期**就 `dlopen` macOS 专有库，抛 `OSError`，所以连 `macpm --version` 都会挂：

- `macpm/ioreport.py:17-18` — `CoreFoundation.framework`、`/usr/lib/libIOReport.dylib`
- `macpm/smc.py:24-25` — `IOKit.framework`、`/usr/lib/libSystem.B.dylib`
- `macpm/processes.py:29-30` — 又是 `CoreFoundation` + `IOKit`

导入链：`macpm.__main__` → `macpm.macpm` → `macpm.processes` → `ctypes.CDLL(...)` → `OSError: dlopen(...)`。
`tests/test_cli.py` 里的 `python -m macpm --version` 用例在 Linux 上会直接失败，这也正好是第一块要改的（把 CDLL 改成**函数内惰性加载**）。

其余 macOS 专有调用（运行期才触发，不会在导入期炸）：

| 位置 | 调用 | Linux 结果 |
|---|---|---|
| `macpm/soc.py:10-13` | `/usr/sbin/sysctl`、`/usr/sbin/ioreg`、`/usr/bin/grep` | 命令不存在 → 返回空串 → SoC 名变成 `Apple Silicon`、核心数回退到 psutil |
| `macpm/soc.py:65-73` | `system_profiler SPDisplaysDataType` | 不存在 → GPU 核心数 `?` |
| `macpm/sampler.py:401-406` | `pmset -g therm` | 不存在 → 恒为 `Nominal`（不报错，静默降级） |
| `macpm/processes.py:218` | `ps -axo pid=,uid=,rss=,time=,ucomm=` | Linux procps 不认 `ucomm`（BSD 专有）→ 整个进程兜底失效 |
| `macpm/netinfo.py:23` | `/sbin/route -n get default` | Linux 没有 `route get` 语义 → 拿不到网关 |
| `macpm/netinfo.py:24` | `/usr/bin/curl` | 多数发行版有；无则公网 IP 失效 |
| `macpm/network.py:29` | `/usr/bin/nettop` | 不存在 → 整个网络视图显示 `nettop unavailable` |

另外 `macpm/macpm.py:124-126` 的 `os.system('clear')` 在 Linux 上可用（依赖 `$TERM`），不是问题。

---

## 3. 模块级复用率与技术路线

| 模块 | 行数 | macOS 机制 | Linux 替代 | 复用率 |
|---|---|---|---|---|
| `macpm/macpm.py` | 1194 | 布局/主题/按键，仅第 1175 行横幅文案提到 macOS | 原样 | **~100%** |
| `macpm/tui.py` | 79 | 纯文本宽度/边框工具 | 原样 | **100%** |
| `macpm/network.py` | 735 | `nettop -L` CSV | 需重写 `NetworkSampler`；`NetworkView`（362–735 行）原样 | UI 100% / 采集 0% |
| `macpm/processes.py` | 623 | IOAccelerator + `/bin/ps` | psutil 直接够用；GPU% 用 fdinfo（仅本用户） | UI 100% / 采集 ~40% |
| `macpm/netinfo.py` | 326 | `route get default` + `curl` | `ip route` / `/proc/net/route` + `urllib` | ~85% |
| `macpm/sampler.py` | 422 | IOReport 4 个订阅 + `pmset` | cpufreq `/sys` + hwmon + PSI；`calc_freq`/`_sane_energy` 逻辑保留 | ~45% |
| `macpm/soc.py` | 210 | `sysctl` / `ioreg` DVFS 表 | `/proc/cpuinfo`、DMI、`/proc/device-tree/model`、`cpu_capacity` | ~15% |
| `macpm/sensors.py` | 133 | 封装 SMC，接口是 `lines()`/`fan_levels()` | 只需换后端为 hwmon/psutil | ~80% |
| `macpm/smc.py` | 309 | AppleSMC IOKit | **删除**，改 hwmon | 0% |
| `macpm/ioreport.py` | 252 | IOReport ctypes | **删除**，改 cpufreq/sysfs | 0% |

合计：**约 50% 行数可原样保留（UI 层接近 100%）**，需要新写的 Linux 采集层约 **1.2–1.5k 行**。

---

## 4. 功能矩阵（逐项）

图例：✅ 可用　🟡 部分可用/需条件　❌ 不可用　🔒 需要 root（或 udev 授权）

| # | 功能 | macOS 数据源 | Linux 替代数据源 | x86_64 | arm64 | root | 说明 |
|---|---|---|---|---|---|---|---|
| 1 | CPU 总使用率 | IOReport/psutil | `psutil.cpu_percent(percpu=True)`、`/proc/stat` | ✅ | ✅ | 否 | |
| 2 | E/P 簇拆分 | IOReport `CPU Stats` | `cpu_capacity`（0–1024）/每核 `cpuinfo_max_freq` 聚类 | 🟡 | 🟡 | 否 | Intel 12 代+ 真有 P/E，可近似还原；AMD/服务器同构 → 只有一簇；ARM big.LITTLE 用 capacity 阈值可分 |
| 3 | 簇平均频率 | IOReport P-state residency + `pmgr` DVFS 表 | `cpufreq/scaling_cur_freq` 均值；有 `stats/time_in_state` 才做加权 | ✅ | 🟡 | 否 | `intel_pstate` 多数情况没有 `time_in_state`，平均频率只能退化为瞬时值；很多 ARM cpufreq 驱动也不提供 residency |
| 4 | 每核视图 | `CPU Core Performance States` | 每核 `/proc/stat` + `scaling_cur_freq` | ✅ | ✅ | 否 | 视图 2 可完整保留 |
| 5 | CPU 功耗 | `Energy Model` | RAPL `intel-rapl:*\/energy_uj` | 🟡🔒 | ❌ | **是** | 内核 5.10 起 `energy_uj` 仅 root 可读（CVE-2020-8694），需 udev 规则给组权限；ARM 无 RAPL |
| 6 | GPU 功耗 | `Energy Model` | `amdgpu hwmon power1_average` / `nvidia-smi` / i915 hwmon | 🟡 | ❌ | 视驱动 | 独显/AMD APU 可以，Intel 核显多数不行，ARM Mali/Adreno/Panfrost 基本没有 |
| 7 | ANE 占用/功率 | `ANE*` Energy Model | **无对应硬件抽象** | ❌ | ❌ | — | 只能删掉或固定 `n/a`；Intel NPU/AMD XDNA 也没有可用计数器 |
| 8 | GPU 利用率 | `GPU Stats`/`GPU Performance States` | amdgpu `gpu_busy_percent`；i915/xe 只有 per-process fdinfo；`nvidia-smi --query-gpu=utilization.gpu` | 🟡 | ❌ | 否/视驱动 | 没有统一接口；AMD 最顺，Intel 核显拿不到全局 busy%（官方 tool 也靠 debugfs/root） |
| 9 | GPU 频率 | GPU Performance States | `gt_cur_freq_mhz` / `pp_dpm_sclk` / devfreq `cur_freq` | 🟡 | 🟡 | 否 | 同上，驱动相关 |
| 10 | CPU/GPU 温度 | AppleSMC | hwmon：`coretemp`/`k10temp`（CPU）、`amdgpu`/`nouveau`（GPU）、`macsmc`（Asahi） | ✅ | 🟡 | 否 | 用 `psutil.sensors_temperatures()` 就能读；ARM 取决于板级 hwmon，服务器常无 |
| 11 | 风扇转速/负载 | AppleSMC `F0Ac/F0Mx` | hwmon `fan*_input`、`psutil.sensors_fans()` | 🟡 | 🟡 | 否 | 台式机/服务器多半没有 fan 传感器；笔记本要看 `thinkpad_acpi`/`asus`/`dell_smm` 等；`fan*_max` 常常不存在，风扇负载条会没有百分比 |
| 12 | 温度峰值 | 内部状态 | 原样 | ✅ | ✅ | 否 | |
| 13 | 热节流状态 | `pmset -g therm` | PSI `/proc/pressure/cpu` + x86 `thermal_throttle/*_count` + `scaling_max_freq < cpuinfo_max_freq` | 🟡 | 🟡 | 部分 | 只能做"是否被限频"的近似判断，没有 Apple 的 `CPU_Speed_Limit` 语义 |
| 14 | 内存用量 | psutil（含 wired/active/inactive） | psutil / `/proc/meminfo` | 🟡 | 🟡 | 否 | **`wired` 在 Linux 上不存在**（`getattr(..., 0)` → 永远 0G）；`active/inactive` 有，但更合理的是换成 `cached`/`buffers`/`slab`/`dirty` |
| 15 | Swap | psutil | 原样 | ✅ | ✅ | 否 | |
| 16 | 磁盘 IOPS/吞吐 | psutil | `/proc/diskstats` | ✅ | ✅ | 否 | 注意按物理盘过滤，否则 dm-*/loop*/分区会重复计数；容器里看到的是宿主机数字 |
| 17 | 网络吞吐 | psutil per-NIC | `/proc/net/dev` | ✅ | ✅ | 否 | 需过滤 `docker0`/`veth*`/`br-*`/`tun*`，否则虚接口重复累加（现有代码只跳过 `lo`） |
| 18 | 进程 CPU/MEM | psutil + `/bin/ps` 兜底 | psutil | ✅ | ✅ | 否 | **比 macOS 更好**：Linux 的 `/proc/PID/stat`、`statm`、`comm` 世界可读，不需要 `ps` 兜底，也不需要 root 就能看到别的用户的 CPU/内存 |
| 19 | 进程 GPU% | IOAccelerator client `accumulatedGPUTime` | i915/amdgpu fdinfo `drm-engine-*`（仅本用户进程）；root 才能读别人的 `/proc/PID/fdinfo` | ❌ | ❌ | **是** | GPU% 列在 Linux 无 root 时只能对自己进程显示，建议整列隐藏或只标 `--` |
| 20 | TCP 状态计数 / UDP 计数 | `nettop` | `/proc/net/tcp`、`tcp6`、`udp`、`udp6` | ✅ | ✅ | 否 | 世界可读，状态列齐全（`01`=ESTABLISHED、`0A`=LISTEN、`06`=TIME_WAIT…），比 nettop 更准 |
| 21 | 监听端口表 + PID | `nettop` | `/proc/net/tcp` inode → 扫 `/proc/*/fd`；或 `ss -tlnp` | 🟡 | 🟡 | 否/是 | 无 root 只能把 inode 映射到自己的进程；别人的只能显示端口没有进程（有 root 就全了） |
| 22 | 每进程流量 | `nettop` 累计字节 | **无对应接口**：`/proc/PID/net/dev` 是 netns 级不是进程级 | ❌ | ❌ | **是** | 只能：root + eBPF（bandwhich/nethogs 路线）/ `ss -ti` 拿 socket 级 `bytes_acked`+`bytes_received`（全部 socket 可读，但只能归属本用户进程）。这张表在 Linux 无 root 下做不出来 |
| 23 | 本机 IP/前缀/网关 | psutil + `route get` | psutil + `ip route show default` 或 `/proc/net/route` | ✅ | ✅ | 否 | 只改 `_read_default_route()` |
| 24 | 公网 IP / 城市 | `curl` + ipinfo.io | 同（建议直接用 `urllib` 去掉 curl 依赖） | ✅ | ✅ | 否 | |
| 25 | SoC 名称/核心数 | `sysctl machdep.*` | x86：`/proc/cpuinfo` `model name` + DMI；ARM：`/proc/device-tree/model` | ✅ | ✅ | 否 | |
| 26 | GPU 核心数 | `ioreg gpu-core-count` | 无可移植来源（NVIDIA 能查 SM 数，amdgpu 报不出来，Intel 要 debugfs） | 🟡 | ❌ | 是 | 建议显示 `?` 或直接去掉这一项 |
| 27 | 界面/主题/按键/布局 | dashing + blessed | 完全相同（纯 Python + curses） | ✅ | ✅ | 否 | 八套配色、四个视图、footer、宽字符裁剪全部保留 |

统计：**完全可用 9 项、部分可用 10 项、不可用 4 项（ANE、跨用户进程 GPU%、跨用户进程流量、GPU 核心数）**，另有 3 项强制 root。

---

## 5. 四个视图的移植结果

| 视图 | 移植后样子 |
|---|---|
| **视图 1（默认）** | 基本完整：E/P 两个 gauge（Intel 混合架构上语义正确，其他平台退化为单簇）、GPU/ANE gauge 二选一（ANE 移除，GPU 视驱动）、内存+swap、温度/风扇面板（hwmon）、磁盘/网络图表、网络身份行。**Power 图表面板在 ARM 上全 `n/a`，在 x86 上需要 root/udev。** |
| **视图 2（每核）** | **完整可用**，是全项目移植价值最高的一屏（Linux 上 per-core 频率/占用本就成熟）。 |
| **视图 3（进程）** | CPU/MEM/USER/PID 完整，且比 macOS 覆盖更全（能读系统进程）；**GPU% 列需要隐藏或只显示本用户进程**。`ps` 兜底代码可以删掉。 |
| **视图 4（网络）** | 摘要框（TCP 状态、UDP 数、速率、IP/网关/公网 IP）**完整可用**；监听表降级为"能看端口、多数进程无名"；**每进程流量表基本做不出来**，建议改成 `ss -ti` 的"按连接速率"表，或整块隐藏。 |

---

## 6. 动手前的具体改造清单

1. **惰性导入（必须先做）**：`ioreport.py` / `smc.py` / `processes.py` 的 `CDLL` 移到函数内或后端模块里，按 `sys.platform` 选择后端；否则 `--version`/`--help` 在 Linux 上直接崩，CI 也没法跑。
2. **抽后端接口**（建议 `macpm/backend/__init__.py` + `macos/` + `linux/`）：
   `soc_info()`、`cpu_clusters()`、`gpu_metrics()`、`energy()`、`temps()`、`fans()`、`sockets()`、`process_gpu()`。
   `sampler.py` 只保留 `calc_freq`、`_sane_energy`、`_energy_available`、`_disk_rates`、`_network_rates` 这些与平台无关的逻辑。
3. **`soc.py`**：`sysctl`/`ioreg` 走 macOS 分支；Linux 分支读 `cpuinfo`/DMI/device-tree/`cpu_capacity`，TDP/带宽表换成"按 CPU 型号或 `cpuinfo_max_freq` 估算"，否则图表百分比没有量纲。
4. **`sensors.py`**：接口不变，`SMC` 换成 `psutil.sensors_temperatures()` + `sensors_fans()`，按 hwmon `name` 字段分类 CPU/GPU（`coretemp`/`k10temp` → CPU，`amdgpu`/`nouveau` → GPU）。
5. **`processes.py`**：删 `_read_ps`（Linux psutil 够用）或改成 `ps -eo pid=,uid=,rss=,times=,comm=`；`read_gpu_times()` 换成 fdinfo 扫描且只扫自己的进程。
6. **`netinfo.py`**：`_read_default_route()` 改 `ip route show default`（回退 `/proc/net/route`）；`curl` 换 `urllib.request`（顺带去掉外部依赖）。
7. **`network.py`**：`NetworkSampler` 用 `/proc/net/{tcp,tcp6,udp,udp6}` + inode→pid 扫描重写；`NetworkView` 不动。per-process 流量表改为"按连接的 `ss -ti` 速率"或删除。
8. **能力探测**：启动时探测一次（RAPL 可读？hwmon 有温度？GPU 驱动是哪个？），把结果传给 UI，决定显示 `n/a`、隐藏列还是隐藏面板。现有代码里的 `*_available` 机制（`sampler.py:239-246`、`processes.py:263`）正好可以复用。
9. **文案/元数据**：`setup.py` 的 `Operating System :: MacOS`、README 的 "Apple Silicon"、`packaging/build.sh` 的 `arm64` 硬校验（第 20-23 行、第 34 行）都要按平台分叉；PyInstaller 在 Linux 上可用，但必须按目标架构各构建一份。

---

## 7. 工作量与阶段划分（粗估）

| 阶段 | 内容 | 产出 | 估时 |
|---|---|---|---|
| P0 | 惰性导入 + 后端骨架 + Linux `soc`/`sampler`（cpu/mem/disk/net）+ 视图 1/2/3 跑通 | 可日常使用的 MVP | 4–6 人日 |
| P1 | hwmon 温度/风扇 + 热节流近似 | Temps & Fans 面板 | 2–3 人日 |
| P2 | 网络视图重写（`/proc/net` + `ss`） | 视图 4 摘要+监听表 | 4–6 人日 |
| P3 | GPU 后端（amdgpu / i915 / nvidia-smi 三选多） | GPU% / 频率 / 功耗 | 5–10 人日 |
| P4 | RAPL 功率 + udev 规则 + 文档 | x86 功率曲线 | 2–3 人日 |

合计 **3–6 周**（MVP 一周内可出）。永久缺口：ANE、跨用户 GPU%、跨用户进程流量、arm64 功率。

---

## 8. 风险与判断

1. **产品定位风险最大**：Linux 上 btop（CPU/内存/磁盘/网络/RAPL 功率，含 root 提示）、nvtop（GPU）、bandwhich/nethogs（进程流量）已经分别覆盖了这些能力，而且都是 C/Rust 实现、开销更低。macpm 的 Python + 1 秒轮询在 Linux 上再去拼这些数据，几乎没有相对优势。
2. **"无 root"是 macpm 的核心卖点，而 Linux 恰好相反**：功率、跨用户进程 GPU/流量这些"最有价值"的数字在 Linux 上被内核权限模型挡住（`energy_uj` 0400、`/proc/PID/fdinfo` 需 ptrace 权限）。移植后必须诚实标注每项功能的权限要求，否则文档会变成误导。
3. **arm64 的碎片化比 x86 严重得多**：同一份代码在 Asahi（有 `macsmc` hwmon、无 GPU/功率）、树莓派（有 `cpu_thermal`、有 devfreq GPU load）、Graviton（几乎什么都没有）上表现完全不同，测试矩阵成本高。
4. **容器/VM 里测试会得到假象**：Docker 里看不到宿主的 `cpufreq`、`hwmon`、`/proc/diskstats`（除非 `--pid=host`/特权），容易把"环境限制"误判成"接口不存在"。评估验证时要用裸机或真实 VM。
5. **结论建议**：如果目标是"在 Linux 上有一个像 macpm 这样好用的 TUI"，性价比不如直接推荐 btop + nvtop；如果目标是"macpm 的界面风格 + 跨平台"，那么按 P0/P1 做（CPU/内存/磁盘/网络/进程/温度）是值得的，**但不要承诺功率和每进程 GPU/流量**，并把 ANE 彻底删掉。

---

## 附：Linux 侧关键路径速查

```
CPU 占用        /proc/stat, psutil.cpu_percent(percpu=True)
CPU 频率        /sys/devices/system/cpu/cpu*/cpufreq/{scaling_cur_freq,cpuinfo_max_freq,stats/time_in_state}
CPU 簇划分      /sys/devices/system/cpu/cpu*/cpu_capacity, topology/, intel hybrid cpu_core/cpu_atom
CPU 功率        /sys/class/powercap/intel-rapl:*/energy_uj          (root, 5.10+)
GPU 利用率      /sys/class/drm/card*/device/gpu_busy_percent        (amdgpu)
                /proc/<pid>/fdinfo/<fd>  drm-engine-*               (i915/xe/amdgpu, 仅本用户)
                nvidia-smi --query-gpu=utilization.gpu,clocks.sm    (NVIDIA)
GPU 频率        /sys/class/drm/card*/gt_cur_freq_mhz, pp_dpm_sclk, /sys/class/devfreq/*/cur_freq
温度            /sys/class/hwmon/hwmon*/temp*_input  + name；psutil.sensors_temperatures()
风扇            /sys/class/hwmon/hwmon*/fan*_input  + fan*_max；psutil.sensors_fans()
热压            /proc/pressure/cpu；/sys/devices/system/cpu/cpu*/thermal_throttle/*_count
内存            /proc/meminfo（MemTotal/MemAvailable/Cached/Buffers/SReclaimable/Dirty）
磁盘            /proc/diskstats（按物理盘过滤）
网络速率        /proc/net/dev（过滤 docker0/veth/br-/tun）
套接字          /proc/net/{tcp,tcp6,udp,udp6}；inode → /proc/*/fd/socket:[inode]
网关            ip route show default | /proc/net/route
机器型号        /proc/cpuinfo, /sys/class/dmi/id/product_name, /proc/device-tree/model
```
