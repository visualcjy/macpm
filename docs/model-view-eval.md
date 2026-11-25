# macpm 大模型视图（model view）可行性评估

评估对象：macpm v3.3.0 的终端视图架构（`macpm/macpm.py` 视图循环、`macpm/gpuview.py`、
`macpm/network.py`、`macpm/blessed_engine.py`、`macpm/theme.py`、`macpm/linux_backend/modelname.py`）。

评估范围：**只读**监控本机 / 本机容器内的模型服务；不启动、不停止、不加载模型；不覆盖远端主机。

> **实现状态（2026-11-25）：P0 + P1 已落地，P2 完成 Splash / oMLX / llama.cpp / ollama。**
> `macpm/serviceregistry.py` 实现零 HTTP 的服务发现（进程指纹 + 本地 TCP 校验 + 手动端点，
> 已适配 Splash.app / oMLX.app 的 bundle 启动方式）；`macpm/modelview.py` 提供列表/详情与
> 单服务卡片；`macpm/modelfeed.py` 在**选中时**每秒轮询一次（1.5 s 超时、1 MiB 上限），
> 已接 Splash `/status`、oMLX `/admin/api/activity`、llama.cpp（`/props` + `/metrics`，
> 未开 `--metrics` 时降级为模型 + slots）、ollama（`/api/ps`，C 档无速率）。
> `recent requests` 在 Splash / oMLX 上以客户端重建（§5.4 的"完整行"）实现；
> 列表的 `CARDS` / `VRAM` 取自 Linux 厂商 GPU 表（无表时隐藏）；
> 形态 C（≥110×26 的八卡网格）已实现，Power/PCIe 卡片按本机可得数据取舍
> （PCIe 在 macOS 不存在，替换为 Context 卡）。
> vLLM / SGLang 的 Prometheus 适配器尚未实现，未轮询的行仍显示 `unverified`。

---

## 0. 结论摘要

1. **可做，而且和 macpm 现有渲染栈匹配**：视图契约（`prime/sample/display`）、后台线程 + TTL
   缓存（`netinfo.py`）、单卡表格（view 5）、pane/focus/offset（view 4）都是现成的；不需要
   Textual 或任何新依赖。
2. **三种形态共用一套数据源**：
   - 形态 A：只发现到一个服务、终端不够宽 → 紧凑单卡（典型 macOS 的 oMLX / ollama / Splash）。
   - 形态 B：多卡 Linux 上发现多个服务 → 上部服务列表（↑/↓ 选择）+ 下部详情。
   - 形态 C：单服务且 ≥110×30 → 八卡网格 + 上下文/内存条 + recent requests（Strata Overview 的等价物）。
3. **常驻 HTTP 开销为零**：服务发现只读进程与本地 TCP；HTTP 只在"选中"时发生。
4. **后端能力分四档**：Splash / Strata `A+`、llama.cpp / vLLM / SGLang `A`、oMLX `B`、ollama `C`；
   卡片按档位显示，缺字段一律 `--`，绝不推断。
5. **成本主要在适配层**（约 800–900 行 + 测试），不在画法。

---

## 1. 参考对象：Strata 的终端布局

Strata（Niko1221/Strata）的交互式 Monitor 基于 Textual，共七个视图（Overview、Serving、
Requests、Engine、Hardware、Logs、Connect），Overview 是单屏多面板。110×30 下的实际骨架：

```text
STRATA  Serving  http://127.0.0.1:8095   IQ3_S 128K            * Reading prompt · 97%  [LIVE]
 [1 Overview] 2 Serving 3 Requests 4 Engine 5 Hardware 6 Logs 7 Connect
 --------------------------------------------------------------------------------------
 Model state  Idle [Reading] Generating Queued Error      35,610 / 36,742 tokens · 97%
 ##################################################################...
 ┌─ Speed ──────┐ ┌─ GPU load ──┐ ┌─ VRAM ──────┐ ┌─ GPU temp ──┐
 │ 820.0 tok/s  │ │ 88 %        │ │ 11.2 / 12GB │ │ 54 C        │
 │ reading...   │ │ RTX 5070    │ │ 1,644 exp.  │ │             │
 │ :*.=@:*.=@:* │ │ ####@####@# │ │ @@@@@@@@@@@ │ │ ==========  │
 └──────────────┘ └─────────────┘ └─────────────┘ └─────────────┘
 ┌─ Power ──────┐ ┌─ PCIe ──────┐ ┌─ CPU ───────┐ ┌─ Disk read ─┐
 ...
 Context fill 36K/128K 28%          Recent requests
 #############....                  TIME  STATUS PROMPT REUSED OUTPUT TOK/S DURATION
 Experts in VRAM 1,644 · 3.2 GB     23:53:40  Done  17,183  0  19,511  50.5  429.8 s
 System RAM  58.5 / 63 GB
 1-7 views  / filter  Space freeze  r refresh  t theme  b browser  ? help  q quit
```

### 1.1 值得借鉴的六条规则

| 规则 | 说明 |
| --- | --- |
| 小卡的统一语法 | `大字数值` / `副标题(口径)` / `sparkline`，一张卡一个模子 |
| 缺失即 `--` | 拿不到的值不猜、不补零；连"总量差分"的首个样本也是 `--` |
| 连接状态机 | `CONNECTING → LIVE → STALE（保留上一帧 + 快照年龄）→ DISCONNECTED`，另有 `AUTH REQUIRED` / `INCOMPATIBLE` / `NO TELEMETRY`，失败指数退避 |
| 单位与量级 | `820.0 tok/s`、`11.2 / 12 GB`、`36K/128K`、`28%` |
| 历史有界 | 每卡最多 32 点历史；重启清空；重启后首样本 `--` |
| 断点降级 | 110 / 80 / 60 / 40 列四档，< 50 列或 < 14 行退化为 compact；60/40 列仍保留选中项 |

### 1.2 不可借鉴的部分

Strata 的 stage 耗时、批窗口行数、层切分（layer split）、专家缓存、PCIe 吞吐等，来自它**自家
引擎**的 `BSTAT`/`PSTAT` 与本机 NVML；第三方后端一律没有。Strata 本身也**不具备监控第三方
后端的能力**——它读的是自家 `/v1/telemetry`（schema v1）与自家 `/metrics`。这一层适配正是
macpm 需要补的。

---

## 2. 目标形态

### 2.1 形态 A：单服务单卡（默认约 100×22）

```text
┌─ MODEL · omlx ───────────────────────────────────────────────────────────────────────────┐
│ Qwen3-8B-4bit   serving   127.0.0.1:8000   ctx 32K/128K 25%              [LIVE · 2s]     │
│                                                                                          │
│  decode   42.6 tok/s  ▂▃▅▆█▆▅▃▂▁   prefill  318 tok/s  ▁▂▂▃▅█▅▃▂                      │
│  ─────────────────────────────────────────────────────────────────────────────────────   │
│  slots   3 / 8      queue 0          KV cache 68%   ██████████████░░░░░░░░               │
│  TTFT 0.31s avg     e2e 4.2s p50      uptime 3h12m   req 1,284   tok 2.1M                │
│  ─────────────────────────────────────────────────────────────────────────────────────   │
│  GPU 78%   11.2 / 16.0 GB    model 8.4 GB (Q4)    experts 1,644 · 3.2 GB                 │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 形态 B：多服务（多卡 Linux）——上列表、下详情

```text
┌─ MODEL SERVICES · 3 detected ──────────────────────────────────────────────────────────────┐
│   MODEL                BACKEND    ENDPOINT         CARDS    VRAM      STATE                │
│ ▶ Qwen3-8B-Instruct    vllm       127.0.0.1:8000   0-3      28.4 GB   live · 3/8 · 42 t/s  │
│   llama-3.1-8B-Q4      llama.cpp  127.0.0.1:9931   5        6.1 GB    unverified           │
│   qwen3-32b (1 loaded) omlx       127.0.0.1:8010   6,7      21.0 GB   unverified           │
└────────────────────────────────────────────────────────────────────────────────────────────┘
┌─ Qwen3-8B-Instruct · vllm · 127.0.0.1:8000 ────────────────────────────────────────────────┐
│  decode  42.6 tok/s ▂▃▅▆█▆▅▃▂   prefill 318 tok/s ▁▂▂▃▅█▅▃▂        ctx 32K/128K 25%        │
│  slots   3 / 8      queue 0      KV cache 68%  ██████████████░░░░░░░░                      │
│  TTFT 0.31s avg     e2e 4.2s p50  uptime 3h12m  req 1,284  tok 2.1M                        │
│  GPU 0-3  78% avg   28.4 GB   422 W    model 8.4 GB (Q4_K_M)   3 procs                     │
└────────────────────────────────────────────────────────────────────────────────────────────┘
 1-6 view  ↑/↓ select  s sort  Space pause  r refresh  ? help
```

**尺寸预算**（30 行）：表头 3 + 列表 `min(N, 8)` + 详情 10–12 + footer 1。
列表区规则：最少 3 行，最多 `max(3, height // 3)`，超出则滚动（保留选中项可见）。
24 行时列表压到 3 行，80 列时行内只留 `model / backend:port / cards / state`。

**列表排序**：默认 GPU 显存降序（同显存按端口升序）；`s` 切换模型名 / 端口 / 卡号。
不做"按卡筛选"：`CARDS` 列已经表达该信息，筛选会让键位再复杂一层。

### 2.3 形态 C：宽屏网格（≥110×30，单服务）

单服务且终端够宽时，用 4×2 的八卡网格 + 下方条带替代 §2.1 的紧凑卡（等价于 Strata 的
Overview 骨架）。每卡 26 列：`大数字` / `副标题(口径)` / `sparkline`。

```text
┌─ Speed ────────────────┐  ┌─ GPU Load ─────────────┐  ┌─ VRAM ─────────────────┐  ┌─ GPU temp ─────────────┐
│ 45.1 tok/s             │  │ 96 %                   │  │ 10.4 / 12 GB           │  │ 59 C                   │
│ now                    │  │ RTX 5070               │  │ model 8.4 GB · Q4      │  │                        │
│ ▁▂▃▅▆█▆▅▃▂▁▂▃▅▆▇█      │  │ █████████████████████▉ │  │ ▇████████████████████▊ │  │ ▁▂▂▃▃▄▄▄▅▅▅▅▄▄         │
└────────────────────────┘  └────────────────────────┘  └────────────────────────┘  └────────────────────────┘
┌─ Power ────────────────┐  ┌─ PCIe ─────────────────┐  ┌─ CPU ──────────────────┐  ┌─ Disk read ────────────┐
│ 116 W                  │  │ Gen4 x16               │  │ 92 %                   │  │ 9.0 MB/s               │
│ of 250 W limit         │  │ to GPU 3,475 MB/s      │  │ 6 cores · 12 threads   │  │ write 0.2 MB/s         │
│ ▅▆▇████████████████▉   │  │ ▁▂▁▃▂▄▁▂▆▃▂▁▂▁▄▂▁▂▃    │  │ █████████████████████▊ │  │ ▁▂▁▂▂▁▃▂▁▂▁▂▃▂▁▁▂▁▂    │
└────────────────────────┘  └────────────────────────┘  └────────────────────────┘  └────────────────────────┘
 Context fill   24.1K / 256K   9%     Recent requests  (derived)
 #################.................   TIME      STATUS  PROMPT  REUSED  OUTPUT  TOK/S  DURATION
 Experts in VRAM   1,644 · 3.2 GB     09:41:02  done     7,183   5,012   1,204   42.1    28.6 s
 ##############..................     09:40:51  done       626       0      11   26.8     4.0 s
 System RAM     38.4 / 64 GB  60%     09:40:33  abort    2,048   2,048      37      --    11.2 s
 ###########################.....     09:40:12  done    12,940  12,940     512   38.4    33.1 s
```

**8 张卡的可得性**：

| 卡片 | 数据来源 | macOS | Linux NVIDIA | AMD / 国产 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Speed | HTTP（各后端） | ✅ Splash / oMLX | ✅ vLLM / SGLang / llama.cpp | 同左 | **ollama 无** → `--` |
| GPU Load | 本机采样 | ✅ IOReport | ✅ nvml / amdgpu | ✅ | 已有（view 1/5） |
| VRAM | 本机采样 | ✅ | ✅ | ✅ | 副标题用 model 大小，不用 Strata 的 experts 计数 |
| GPU temp | 本机采样 | ✅ SMC | ✅ hwmon | ✅ | 已有 |
| Power | 本机采样 | ✅ 功率，**无上限** | ✅ 含 `power.limit` | 部分有 | "of 250 W limit" 仅 NVIDIA |
| PCIe | 本机采样 | ❌ 无 PCIe 总线 | ✅ 代宽已有；吞吐需 `dmon -s t` | ❌ 仅代宽/宽度 | 见 5.3 之外的上一节结论 |
| CPU | 本机采样 | ✅ | ✅ | ✅ | 已有 |
| Disk read | 本机采样 | ✅ | ✅ | ✅ | 已有（读写双值） |

**尺寸预算**：标题 1 + 两排卡 10 + 条带 6 + footer 1 = 18 行；若同时保留 §2.2 的列表再加 7 行，
共 25 行，因此只在 ≥30 行终端启用，否则回落 §2.1 紧凑卡。

**取舍**：8 张里 5 张（GPU / VRAM / 温度 / 功耗 / CPU / 磁盘）与 view 1、view 5 重复。重复是为了
"单屏自包含"，只在宽屏才值得；紧凑形态不复制这些卡。

---

## 3. 服务发现（零 HTTP）

### 3.1 有效性判定

"只列能识别和有效的服务"，但有效性判定**不能依赖 HTTP**，否则每条新服务都要先发请求，与
"尽量减少系统负担"矛盾。改为纯本地三重判定：

| 判据 | 手段 | 成本 |
| --- | --- | --- |
| 进程存活 | 现有采样（`psutil` / `/proc`） | 0 |
| 启动命令命中已知后端 | `cmdline` 指纹表（见 3.2） | 读一次 cmdline，缓存 10 s |
| 端口在监听 | 本地 `connect()`（127.0.0.1:port）只做 TCP 握手 | ~0.1 ms |

三者都过才进列表。HTTP 只在**选中时**发生，之后按 1 s 轮询。

### 3.2 指纹表

| 特征（cmdline） | 后端 | 默认端口 |
| --- | --- | --- |
| `vllm serve` / `-m vllm.entrypoints…` | vLLM | 8000 |
| `sglang.launch_server` / `-m sglang.launch_server` | SGLang | 30000 |
| `llama-server` | llama.cpp | 9931（旧版 8080） |
| `ollama serve` | ollama | 11434 |
| `omlx serve` | oMLX | 8000 |
| `splash serve` | Splash | 8000 |

### 3.3 进程 → 服务

| 步骤 | 数据来源 | 产出 |
| --- | --- | --- |
| 1 | GPU 采样（复用 `gpu_process_source()`，**共享缓存，不重复调 SMI**） | 持有显存的 PID + `pid → 卡` 映射 |
| 2 | `psutil.Process(pid).cmdline()`（macOS/Linux 通用；Linux 侧 `proc.py` 已有 `/proc/<pid>/cmdline` 读取） | 后端类型、`--port`、`--served-model-name`、`--model`、`--tp-size` |
| 3 | 父链回溯（现成的 `linux_backend/modelname.py:resolve`） | 合并 launcher + worker 进程组，得到"一个服务" |
| 4 | `/proc/<pid>/environ`（root 或同用户） | `CUDA_VISIBLE_DEVICES` 校验卡集 |
| 5 | HTTP 指纹（仅选中项） | 能力档位 + 实时指标 |

**去重主键**：`(backend, port)`。vLLM 的 TP=8 会展开成 9 个进程共用同一端口，父链回溯后归为
一条；`pid` 只用于判断"是否重启过"。

**跨平台**：同一套 registry 在 macOS 上用 psutil 也能工作（`cmdline` 可读），卡集退化为单卡；
此时列表通常只有 0–2 行，视觉上就是形态 A。

---

## 4. 统一数据模型

适配层产物，所有字段可为 `None`（渲染为 `--`）：

```text
ModelCard(family, backend, endpoint, model, state, loaded_count,
          slots_used, slots_total, queue, kv_used, kv_total,
          tps_decode, tps_prefill, ttft_s, e2e_s,
          req_total, tok_total, uptime_s, stale_s, error)
```

`family` 表示服务所属的协议族（见 5.2），取值 `llamacpp` / `prometheus` / `kserve` /
`native` / `openai`，决定用哪个解析器与哪套指标名字典；渲染层不感知它。

### 4.1 口径陷阱（照抄 Strata 文档的警告）

| 陷阱 | 处理 |
| --- | --- |
| in-flight ≠ occupied slots | 分别显示，**绝不相加** |
| 引擎侧首 token ≠ 客户端 TTFT | 标注为"引擎侧"，不要与 bench 的客户端 TTFT 混用 |
| 差分求 tok/s | 首样本、切换服务、服务重启后显示 `--` |
| `kv_cache_usage_perc` 的量纲 | 按实测值判断 0–1 还是 0–100，不硬编码 |
| llama.cpp 的 `n_ctx` | `--kv-unified` 下是共享池，不能直接乘 slots |
| ollama 的 `size` vs `size_vram` | 前者含 CPU 部分，用 `size_vram / size` 表达 GPU 占比 |

---

## 5. 后端能力分档与协议族

| 档 | 后端 | 关键端点 | 能拿到 | 拿不到 |
| --- | --- | --- | --- | --- |
| **A+**（参考实现） | Strata | `/v1/status`、`/v1/telemetry`（schema 1）、`/metrics` | 模型状态、请求/生效 slots、占用槽位、引擎排队、引擎侧首 token、每卡 VRAM 趋势、层切分、批窗口、请求转移记录 | 引擎内部字段（BSTAT/PSTAT、专家缓存）不进最小集；`/v1/telemetry` 需 telemetry 只读密钥 |
| **A+** | Splash | `GET /status`、`GET /metrics`、`GET /v1/models`、`/health`、`/ready` | `requests.*` 计数、`active_requests[].phase`（queued / waiting_resources / waiting_prefix / prefill / decode / waiting_mask）、`metrics.decode_tokens_per_second`、`metrics.decode_gap_ms` 分位、`latency` 分阶段 TTFT、`maximum_context_tokens`、`memory_actual.*`、`thermal_state`、`kv_spill` | 各请求的逐 token 速率（需差分 `generated_tokens`） |
| **A** | llama.cpp | `/props`、`/slots`、`/metrics`（需 `--metrics`）、`/v1/models` | `total_slots`、`requests_processing` / `requests_deferred`、`predicted_tokens_seconds` / `prompt_tokens_seconds`（现成 gauge）、`n_tokens_max`、每槽 `n_ctx` / `is_processing` / `n_decoded`、模型 `n_ctx_train` / `n_params` / `size` | TTFT 直方图（只有累计 timings） |
| **A** | vLLM | `/metrics`、`/v1/models`、`/version` | `num_requests_running` / `waiting`、`kv_cache_usage_perc`、`prompt_tokens` / `generation_tokens` 计数器、`time_to_first_token_seconds` / `e2e_request_latency_seconds` 直方图 | **无 tok/s gauge，必须差分计数器** |
| **A** | SGLang | `/metrics`、`/get_server_info`、`/v1/models` | `num_running_reqs` / `num_queue_reqs`、`gen_throughput`（现成 gauge）、`token_usage`、`kv_used_tokens` / `max_total_num_tokens`、TTFT / e2e 直方图 | — |
| **B** | oMLX | `/admin/api/activity`、`/admin/api/stats`、`/admin/api/models`、`/admin/api/device-info` | 每个已加载模型的 `active_requests` / `waiting_requests`、`prefilling[]`（`processed` / `total` / `speed` / `eta`）、`generating[]`（含**每请求 `tokens_per_second`**）、`idle_seconds`、`ttl_remaining_seconds`、`model_memory_used/max` | 无 Prometheus `/metrics`；接口需**主 API Key**（Bearer），loopback + 关认证可免 |
| **C** | ollama | `/api/ps`、`/api/tags`、`/api/version` | 模型名、`size`、`size_vram`、`expires_at`、`details.quantization_level` / `parameter_size` | **无 tok/s、无并发/队列、无 KV、无延迟**；tok/s 只能来自推理响应（违反只读） |

### 5.1 各档在卡面上的差异（形态 A 的详情卡）

| 档 | 额外显示 | 固定显示 `--` |
| --- | --- | --- |
| A+ | phase 分布（`decode 2 / prefill 1`）、TTFT 分段、thermal、KV spill | — |
| A | 全量；vLLM 的 tok/s 延迟 2 s 出数 | — |
| B | active/waiting、prefill 进度、idle/TTL；model 列显示"当前模型 (N loaded)" | TTFT / e2e（无直方图） |
| C | 模型、`size_vram`、卸载倒计时 | tok/s、并发、KV、延迟 |

**ollama 处理**：按 C 档如实显示，卡头标 `ollama · 基础`，避免被当成 bug。

**oMLX 多模型处理**：只看当前模型。判定三级：

```text
1. 有 active/waiting 请求的（多个时取 generating 中 tokens_per_second 最高 / 请求最老的）
2. 没有请求时：idle_seconds 最小的（最近使用过）
3. 都没有：第一个已加载模型，卡头标 "idle"
```

卡头附 `N loaded` 提示还有别的模型；`n` 可轮换查看，但**默认只显示当前**。

**Splash 的两条路径**：

1. 独立运行 `splash serve`：默认 `127.0.0.1:8000`，`--api-key` 默认关闭；一旦开启，除 `/`、
   `/health`、`/ready` 外全部需要 `Authorization: Bearer`（或 `x-api-key`）。
2. 作为 oMLX 的外部引擎运行：oMLX 用**随机 loopback 端口 + 随机 API Key**（key 只走环境变量）
   拉起 `splash serve`，外部无法直接访问。此时不写 Splash 适配器，直接读 oMLX
   `/admin/api/activity`——它已经把 Splash 的 prefill 进度与生成速度转成同样的行。

### 5.2 协议族（决定适配器的边界）

新增一个后端**不应**等于"再写一个适配器"，先归族（共五族）：

| 协议族 | `family` | 代表 | 判定方式 | 新产品成本 |
| --- | --- | --- | --- | --- |
| llama.cpp 家族 | `llamacpp` | `llama-server`、`llamafile`、Jan、LM Studio 的 GGUF 引擎、部分 KoboldCpp | `GET /props` 返回 200 | **0**（复用同一解析） |
| Prometheus 家族 | `prometheus` | vLLM、SGLang、TGI、LMDeploy、TensorRT-LLM、Triton、OVMS、LocalAI、Xinference、NIM | `/metrics` 文本里出现对应前缀 | 20–60 行指标名映射表 |
| KServe 家族 | `kserve` | Triton、OVMS 的模型面 | `/v2/health/ready`、`/v2/models/{name}/stats` | ~100 行（另一套协议） |
| 自有 JSON | `native` | oMLX `/admin/api/*`、Splash `/status`、LM Studio `/api/v0/models`、llama-swap `/running` | 端点存在且 JSON 字段可识别 | 60–150 行 |
| 仅 OpenAI 兼容 | `openai` | `mlx_lm.server`、text-generation-webui、Docker Model Runner、`llama-cpp-python` | 只有 `/v1/models` + `/health` | ~10 行（C 档） |

一个产品可以同时属于多个族（Triton = Prometheus + KServe），取**信息最全**的那个解析；
适配器内部的指标名一律"多别名 + 缺失降级"。

### 5.3 可扩展服务清单（未实现，按收益/成本排序）

| 优先级 | 服务 | 档 | 依据（已核对） | 备注 |
| --- | --- | --- | --- | --- |
| P1 | **TGI**（HF） | A | 官方文档明示 `/metrics` Prometheus 端点（`tgi_request_count`、`tgi_queue_size`、`tgi_batch_current_size` 等） | 企业/云上最常见，字段最清晰 |
| P1 | **LM Studio** | B | `/api/v0/models`（已加载状态、上下文、量化）+ `/v1/models` | 桌面用户最多；无 Prometheus，只能到 B |
| P1 | **llama-swap** | **B+** | `/running`（当前运行哪些模型）、`/metrics`（系统与 GPU）、`/health`、`/api/profiles` | 多模型热切换场景的**最优入口**，见下方判断 |
| P2 | **Strata** | **A+** | `/v1/status`、`/v1/telemetry`（`schema_version: 1`）、`/metrics` | 自带 TUI，只服务自家引擎；见下方判断 4 |
| P2 | **LMDeploy** | A | 源码 `mount_metrics` + `enable_metrics` 开关 | 国内部署常见；需启动时开启 |
| P2 | **LocalAI** | B | `core/http/app.go` 内置 Prometheus metrics 服务与中间件 | 指标偏 HTTP/系统层，token 级较少 |
| P2 | **Xinference** | B | `aioprometheus` registry + `/metrics`（可关闭） | 同上，偏通用指标 |
| P3 | **TensorRT-LLM**（`trtllm-serve`） | A | `add_api_route("/metrics", …)` + `prometheus_client` multiprocess | 指标多且版本间变动大 |
| P3 | **Triton** | A | `/metrics` + `/v2/models/{name}/stats` + `/v2/health/ready` | KServe 协议，不是 OpenAI |
| P3 | **OpenVINO Model Server** | A | `/metrics` + KServe | 同上，边缘/CPU 场景 |
| 免费 | **NVIDIA NIM** | A | 容器内即 vLLM/TGI | 端点一致，零成本 |
| 免费 | **llamafile / Jan 内置 server** | A | llama.cpp server 翻版 | 同一适配器；桌面版端口不稳定时靠进程发现兜底 |
| C | `mlx_lm.server`、text-generation-webui、Docker Model Runner、`llama-cpp-python` | C | 只有 `/v1/models`；`llama-cpp-python` 无 `/props`、`/slots` | 只显示模型名 + 在线状态 |

**三个判断**：

1. **管理器入口比引擎入口更稳**。llama-swap 与 oMLX 是同一角色：知道"当前加载哪个模型、
   上游端口、TTL"。对"多模型只看当前"这条需求，`--model-url` 指向管理器比指向引擎抗重启。
2. **Triton / OVMS 是 KServe 协议**，不要混进 OpenAI 适配器；`family` 字段负责隔离。
3. **不建议纳入**：Open WebUI / Flowise / LibreChat（前端而非引擎）、LiteLLM（代理，指标
   反映代理层而非本机服务）、GPT4All / Jan 桌面版（内部引擎端口不稳定，走进程发现更可靠）。
4. **Strata 纳入，但只作 P2**：它自带 TUI，且只服务自家引擎与模型包；纳入的理由是它的
   `/v1/telemetry` 有 schema 版本、字段最全（slots / 占用 / 排队 / 每卡 VRAM 趋势 / 层切分），
   正是本方案卡片布局的参考实现，适配成本低，还能反证我们的数据模型。但它**不占优先级**，
   先让通用后端（A/B/C 档）跑通。

**这类扩展的成本与风险**：

| 项 | 说明 |
| --- | --- |
| 指标名漂移 | TGI 0.x→3.x 改过名，TRT-LLM 几乎每版新增；映射表必须多别名 + 缺失 `--` |
| 启动开关 | LMDeploy 需 `enable_metrics`；TGI 的 `/metrics` 可能被反代挡掉；Triton 的 metrics 常在独立端口 8002 |
| 口径不可比 | TGI 的 `queue_size` 含 batch 内、Triton 的统计是累计计数，跨后端不做横向对比 |
| 认证差异 | LM Studio 无鉴权；Triton 通常无鉴权但有独立 metrics 端口；NIM 常带 key |

### 5.4 recent requests：能做什么、不能做什么

Strata 那张表来自它自己服务端的内存遥测（保留 128 条转换、快照下发 32 条）。第三方后端没有这个
接口，只能在客户端按 1 s 采样重建请求生命周期：`request_id` 出现时记开始时间与初始 token 数，
消失即视为完成，据此算时长与 tok/s，写入环形缓冲。

| 列 | Strata | Splash | oMLX | llama.cpp | vLLM / SGLang | ollama |
| --- | --- | --- | --- | --- | --- | --- |
| TIME | ✅ | ✅ | ✅ | ✅ | ✅（区间） | — |
| STATUS | ✅ | ✅ 完成/取消/失败 | ⚠️ 推断 | ⚠️ 推断 | ⚠️ `finished_reason` 计数 | — |
| PROMPT | ✅ | ✅ `prompt_tokens` | ✅ | ⚠️ 可能 `--` | ❌ | — |
| REUSED | ✅（缓存命中） | ⚠️ 部分 | ❌ | ❌ | ❌ | — |
| OUTPUT | ✅ | ✅ `generated_tokens` | ✅ | ✅ `n_decoded` | ❌ | — |
| TOK/S | ✅ | ✅ | ✅ 每请求 `tokens_per_second` | ⚠️ 差分 | ❌ | — |
| DURATION | ✅ | ✅ `age_ms` | ✅ | ⚠️ 差分 | ❌ | — |

三档保真度：

- **完整行**：Strata、Splash、oMLX。
- **半行**：llama.cpp（`/slots` 的 `id_task` / `n_decoded`；PROMPT 需从 `/metrics` 差分，可能 `--`）。
- **仅汇总**：vLLM、SGLang —— 不出表，降级成一行
  `last 60s: 42 done · 2 abort · p50 4.2s · p95 11.8s`，来源是计数器与直方图，不伪造行。
- **无**：ollama。

约束：

- 表头固定标 `(derived)`：1 s 采样意味着**亚秒请求会完全漏采**，时长有 ±1 采样周期误差，
  部分后端的 STATUS 只能推断。
- 环形缓冲 8–16 行，只在视图打开期间保留；切换服务、服务重启即清空（与 Strata 的
  "实例重启清空"一致）。
- 不显示 prompt 正文，不记录答案文本。

### 5.5 日志 / 输出 / OTLP：评估过，决定不做

结论：**默认不走日志**。理由是"改启动参数（侵入）+ 定位并跟踪日志或跑 collector（负载与维护）
+ 格式随版本漂移 + 可能把 prompt 原文带进终端"，而换来的字段并不比 HTTP 多。

| 来源 | 能给 | 开关 | 稳定性 | 结论 |
| --- | --- | --- | --- | --- |
| 访问日志 | TIME / STATUS / DURATION | 通常默认有 | 中（随框架变） | 只补 3 列，收益不足 |
| 请求级日志 | 请求 id + 起止，token 数常年缺失 | 需重启（vLLM `--enable-log-requests` 默认关） | 低 | 否 |
| 结构化遥测（vLLM OTLP） | 完整：prompt / generation tokens、TTFT、排队 | 需改启动参数 + 接收端 | 高 | 唯一完整方案，但侵入且重，否 |

逐后端事实：

| 后端 | 日志里有什么 | 为什么不用 |
| --- | --- | --- |
| vLLM | 每几秒一行 `Avg prompt throughput … Running/Waiting/KV`；`--enable-log-requests` 可打请求日志（含 prompt 原文）；`--otlp-traces-endpoint` 出 span | stats 行与 `/metrics` 等价；请求日志泄漏 prompt；OTLP 要 collector |
| SGLang | `--log-level`；周期性 `Decode batch … token usage` | 无 per-request 开关；日志同样是 `/metrics` 的劣化版 |
| ollama | gin 访问日志（状态码 + 整请求耗时）；`OLLAMA_DEBUG`；`OLLAMA_DEBUG_LOG_REQUESTS` 落盘请求体 | 无 token 数；debug 输出非结构化；落盘文件不是事件流 |
| llama.cpp | `--log-file` 可写日志，含每请求 timings | 与 `/metrics`、`/slots` 重复，无额外收益 |

读日志本身的代价（常被低估）：

| 问题 | 后果 |
| --- | --- |
| 日志位置不定 | stdout 可能指向 systemd journal、docker、文件重定向或前台终端 |
| `/proc/<pid>/fd/1` | 是 **pipe** 时读取会抢走服务端写出的字节、甚至让写端阻塞；只有重定向到普通文件才可安全 tail |
| 轮转 / 截断 | 需处理 inode 变化与续读位置 |
| 多进程 | vLLM TP 每个 worker 各写一份，去重困难 |
| 并发交错 | 没有 request id 无法配对起止 |
| 隐私 | 请求级日志含 prompt 原文，违反"不显示 prompt"原则 |

**决定**：不做日志旁路、不做 OTLP 接收端（见附录 C 决策 9）。recent requests 只保留 5.4 的
"客户端重建 + 聚合降级"，ollama / vLLM / SGLang 上如实显示 `--` 或汇总行。

---

## 6. 探测预算

| 时机 | 对象 | 请求 |
| --- | --- | --- |
| 全程，每 tick | 候选进程（有 GPU 的几十个） | **0 个 HTTP**：只读 cmdline/env + 本地 TCP 握手 |
| 选中 | 该服务 | 1 个：Splash `/status`；vLLM/SGLang/llama.cpp `/metrics`（llama.cpp 加 `/slots`）；oMLX `activity`；ollama `/api/ps` |
| 切换选中 | 新选中项 | 立即全量探一次，并清空差分基线 |
| 其他 | — | **不探**（未探测行 STATE 显示 `unverified`） |

约束：超时 **1.5 s**（比 `netinfo._http_get` 的 3 s 更短，因为它每秒都要发一次）；响应上限
**1 MiB**（沿用 `netinfo._http_get` 的 `read(1 << 20)`）；只在该视图可见时轮询。

**`VRAM` / `CARDS` 两列不依赖 HTTP**，来自 GPU 采样器，所以未探测的行也是满的。

---

## 7. 状态机与选中态

```text
unverified ──(选中/探测成功)──▶ live
      │                         │
      │(端口关/进程退)           ├─▶ no-telemetry（只有 /v1/models，如 ollama）
      └──────────────▶ gone     ├─▶ auth（401/403）
                                └─▶ stale（连续超时，保留最后快照 + 年龄）
```

- 选中态按 `(backend, port)` 绑定，不用行号。
- 服务重启（pid 变、端口不变）：行保留、选中保留、历史清空、标 `restarted`。
- 服务消失：行保留 3 s 并标 `gone`，然后移除并自动选相邻行。
- 列表抖动由上述身份绑定 + 宽限期解决。

---

## 8. 键位

```text
1-6 视图   ↑/↓ 选择服务（仅形态 B；形态 A 下无操作）   s 排序
Space 冻结  r 立即重探   ? 帮助   q 退出
```

与现有视图无冲突：view 3/4/5 的 ↑/↓ 只在各自视图生效，主循环按 `view == 6` 分支即可。

---

## 9. 渲染与降级

| 条件 | 行为 |
| --- | --- |
| ≥ 100 列 | 全量（decode + prefill 双通道 sparkline） |
| 80–99 列 | 去掉 prefill 通道与 e2e |
| 60–79 列 | 只留 decode / slots / KV / 状态 |
| < 60 列 | 只留列表（形态 B）或单行摘要（形态 A） |
| ≥ 26 行 | 列表 + 详情 |
| 16–25 行 | 列表 3 行 + 详情 5 行（decode / slots / KV） |
| < 16 行 | 只列表 + footer |

渲染方式：**不扩展 tile 引擎**，像 view 3/4/5 一样按行拼字符串（`tui._fit` / `_pad` 已有），
成本最低、窄宽度可控。颜色沿用 native 主题角色：`RATE`（蓝）给 tok/s、`ADDRESS`（青）给端点、
`WARN`/`CRIT`（黄/红）给 KV 压力与超时。

---

## 10. 与 GPU 视图（key 5）的分工

| | GPU 视图 | 模型视图 |
| --- | --- | --- |
| 视角 | 卡 → 进程 | 服务 → 卡 |
| VRAM | 每卡总量 / 每进程 | 每服务占用（其 PID 合集） |
| 强项 | 利用率、温度、功耗、卡名 | tok/s、并发、KV、TTFT |

必须共享：**SMI 调用缓存**。`nvidia-smi` 单次 100–300 ms，两个视图各调一次会显著拖慢多卡机；
模型视图只读 GPU 视图已经在刷的缓存（`linux_backend/gpu.py` 的 `smi_rows` 有 TTL），或由
`begin()` 传入同一个 sampler。

---

## 11. 边界与限制

- **macOS**：registry 用 psutil 可跑；卡集退化为单卡；形态通常是 A。
- **无 GPU 的 Linux / 容器**：只要能读 cmdline 就能列出服务，`CARDS` 显示 `-`。
- **其他用户的进程**：无 root 读不到 `environ` / `cmdline` → 行显示 `unreadable (root)`，
  与进程视图的 `n/a` 风格一致。
- **容器**：`containerd-shim` 之下的父链回溯 `modelname` 已处理；k8s 的 `bash -c "…"`
  拼接也有处理。
- **远端 / 多机**：不在范围，列表只列本机（与 Strata 的 connection/prefix 体系不同，刻意如此）。
- **认证**：API key 优先读各后端惯用环境变量（`SPLASH_API_KEY`、`VLLM_API_KEY` 等），
  `--model-key` 兜底；不写进配置、日志、标题栏。
- **手动条目**：`--model-url` 追加的固定条目始终置顶并标 `manual`，发现失败时视图不至于全空。

---

## 12. 工作量与分期

| 阶段 | 内容 | 估时 |
| --- | --- | --- |
| P0 | `macpm/serviceregistry.py`：cmdline 指纹 + 父链合并 + 本地 TCP 校验 + 假 `/proc` fixture 测试（**全程不碰 HTTP 即可先跑起来**） | 1–1.5 天 |
| P1 | 列表 + 选中态 + 详情卡接线；A 档（llama.cpp / vLLM / SGLang） | 2–3 天 |
| P2 | Splash（A+）、oMLX 当前模型（B）、ollama（C） | 1.5–2 天 |
| P3 | 窄终端降级、文档、截图 | 1 天 |

代码规模粗估：

| 模块 | 行数 |
| --- | --- |
| `serviceregistry.py`（扫描/指纹/合并/卡集/缓存） | 250–300 |
| 列表渲染（表头、行格式、宽度降级、滚动、选中高亮） | 150–200 |
| 选中态管理（身份绑定、消失/重启、自动跳行） | ~80 |
| 详情卡渲染（三档宽度、sparkline、进度条） | 300–400 |
| 适配器（Splash / llama.cpp / vLLM / SGLang / oMLX / ollama / LM Studio / llama-swap） | 各 60–150；Prometheus 族新产品多为 20–60 的映射表 |
| 接线（`begin()` 分支、键位、footer、参数） | ~50 |
| 宽屏网格渲染（形态 C，复用 tile 引擎） | ~120 |
| 请求跟踪（环形缓冲 + 身份绑定 + 采样差分） | ~150 |
| 聚合降级（vLLM / SGLang 汇总行） | ~40 |
| 测试（解析固件、渲染快照、超时/401/缺字段） | 250–350 |

> 5.3 中的 P2/P3 扩展服务不计入上表：按每个服务约 0.5 天追加，且不阻塞主线。
> Prometheus 族内部共享同一个解析器与缓存层，只有指标名字典是逐个产品累加的。

---

## 13. 风险

| 优先级 | 风险 | 缓解 |
| --- | --- | --- |
| 高 | 服务启动方式千奇百怪（systemd、docker、k8s、`sh -c` 嵌套、`uv run`）导致漏发现 | `--model-url` 手动条目置顶标 `manual`；指纹表可扩展 |
| 高 | 多卡机上 `nvidia-smi` 被两个视图重复调用 | 共享 sampler / SMI TTL 缓存 |
| 中 | 指标名与语义随版本漂移（vLLM `gpu_cache_usage_perc` → `kv_cache_usage_perc`；llama.cpp `--metrics` 默认关、`--no-slots`；TGI 0.x→3.x 改名；TRT-LLM 每版新增） | 多别名探测 + 缺失降级 + 指纹只认端点存在性 |
| 中 | 列表抖动 / 端口复用导致行闪烁 | `(backend, port)` 身份 + 3 s 宽限 + `restarted` 标记 |
| 中 | ollama 空卡被误认为 bug | 卡头标档位（`ollama · 基础`） |
| 中 | recent requests 是客户端派生数据，容易被误当服务端精确历史 | 表头固定标 `(derived)`；漏采与 ±1 周期写进说明；不显示 prompt 正文 |
| 低 | 每秒抓 `/metrics` 在大集群上偏重 | 只在选中时探、1.5 s 超时、1 MiB 上限、视图不可见不轮询 |

---

## 附录 A：端点速查

### A.1 已在方案内的后端

| 后端 | 探测（一次性） | 周期（选中后） | 认证 |
| --- | --- | --- | --- |
| Splash | `GET /status` | `GET /status` | `--api-key`（默认关） |
| llama.cpp | `GET /props` | `GET /metrics` + `GET /slots` | 可选 `--api-key` |
| vLLM | `GET /v1/models` | `GET /metrics` | 可选 `--api-key` |
| SGLang | `GET /get_server_info` | `GET /metrics` | 可选 `--api-key` |
| oMLX | `GET /v1/models` | `GET /admin/api/activity` | 主 key（Bearer） |
| ollama | `GET /api/version` | `GET /api/ps` | 无 |

### A.2 可扩展服务（未实现，见 5.3）

| 后端 | 族 | 探测（一次性） | 周期（选中后） | 认证 |
| --- | --- | --- | --- | --- |
| TGI | `prometheus` | `GET /info` | `GET /metrics` | 可选 token |
| LM Studio | `native` | `GET /api/v0/models` | 同上 | 无 |
| llama-swap | `native` | `GET /running` | `/running` + 上游 `/metrics` | 可选 API key |
| LMDeploy | `prometheus` | `GET /v1/models` | `GET /metrics`（需 `enable_metrics`） | 可选 |
| LocalAI | `prometheus` | `GET /v1/models` | `GET /metrics` | 可选 |
| Xinference | `prometheus` | `GET /v1/models` | `GET /metrics`（可关闭） | 可选 |
| TensorRT-LLM | `prometheus` | `GET /v1/models` | `GET /metrics` | 可选 |
| Triton | `kserve` | `GET /v2/health/ready` | `/metrics` + `/v2/models/{name}/stats` | 通常无 |
| OVMS | `kserve` | `GET /v2/health/ready` | `/metrics` | 通常无 |
| Strata | `native` | `GET /v1/status` | `GET /v1/telemetry`（schema 1）+ `/metrics` | telemetry 只读 key（`STRATA_TELEMETRY_KEY`） |
| `mlx_lm.server` / text-generation-webui / Docker Model Runner / `llama-cpp-python` | `openai` | `GET /v1/models` | 同左（只显示在线状态） | 无 |

## 附录 B：参考链接

- Strata 终端文档：<https://github.com/Niko1221/Strata/blob/main/docs/TERMINAL.md>
- Strata 遥测字段矩阵（schema 1）：<https://github.com/Niko1221/Strata/blob/main/docs/TERMINAL_TELEMETRY_FIELDS.md>
- oMLX Admin API：<https://github.com/jundot/omlx/blob/main/docs/admin-api.md>
- oMLX 外部引擎（Splash）：<https://github.com/jundot/omlx/blob/main/docs/Splash.md>
- Splash API（`/status`、`/metrics`）：<https://github.com/incoai/splash/blob/main/docs/api.md>
- Splash 服务参数（`--api-key`、默认端口）：<https://github.com/incoai/splash/blob/main/docs/server.md>
- llama.cpp server（`/props`、`/slots`、`/metrics`）：<https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>
- ollama API（`/api/ps`）：<https://github.com/ollama/ollama/blob/main/docs/api.md>
- vLLM 指标定义：<https://github.com/vllm-project/vllm/blob/main/vllm/v1/metrics/loggers.py>
- SGLang 指标参考：<https://docs.nvidia.com/aiperf/server-metrics/ai-perf-server-metrics-reference>

扩展服务（5.3）：

- TGI 指标文档：<https://github.com/huggingface/text-generation-inference/blob/main/docs/source/reference/metrics.md>
- LMDeploy API 服务（`enable_metrics`）：<https://github.com/InternLM/lmdeploy/blob/main/lmdeploy/serve/openai/api_server.py>
- TensorRT-LLM `trtllm-serve`（`/metrics`）：<https://github.com/NVIDIA/TensorRT-LLM/blob/main/tensorrt_llm/serve/openai_server.py>
- llama-swap（`/running`、`/metrics`）：<https://github.com/mostlygeek/llama-swap>
- LocalAI HTTP 层（Prometheus 中间件）：<https://github.com/mudler/LocalAI/blob/master/core/http/app.go>
- LM Studio REST API：<https://lmstudio.ai/docs/developer/rest>
- Triton 指标：<https://github.com/triton-inference-server/server/blob/main/docs/user_guide/metrics.md>
- OpenVINO Model Server：<https://github.com/openvinotoolkit/model_server>
- Xinference REST API：<https://github.com/xorbitsai/inference/blob/main/xinference/api/restful_api.py>

## 附录 C：已定决策

| # | 决策 | 理由 |
| --- | --- | --- |
| 1 | ollama 按 C 档如实显示，不补数据 | 只读接口拿不到 tok/s；补数据需发推理请求，违反只读 |
| 2 | oMLX 只看当前模型，卡头标 `N loaded` | 多模型同时驻留，单卡放不下；`activity` 有 `idle_seconds` 可判定 |
| 3 | Splash 独立时单独适配；挂在 oMLX 下时不单独适配 | 后者端口与 key 随机，只能经 oMLX 读取 |
| 4 | 认证信息只走环境变量 / `--model-key`，不落盘 | 与 macpm"无需密码"的定位折中 |
| 5 | 未探测服务留空显示 `unverified` | 用户要求尽量减少系统负担 |
| 6 | 不做按卡筛选，只用 `CARDS` 列表达 | 保留键位简单性 |
| 7 | 按协议族（`llamacpp` / `prometheus` / `kserve` / `native` / `openai`）适配，不按产品逐个适配 | 新产品 20–60 行，避免 N 个一次性适配器 |
| 8 | 多模型场景优先把 `--model-url` 指向管理器（llama-swap / oMLX），而非引擎 | 管理器知道"当前模型 / 上游端口 / TTL"，抗重启 |
| 9 | 不做日志旁路与 OTLP 接收端 | 需改启动参数（侵入）、定位并跟踪日志或跑 collector（负载与维护），且格式不稳、可能泄漏 prompt |
| 10 | recent requests 只在客户端重建，表头固定标 `(derived)` | 服务端无 per-request 历史；1 s 采样会漏亚秒请求，必须显式标注 |
| 11 | Strata 纳入支持，但只作 P2 | 自带 TUI 且只服务自家引擎；纳入是因为其 telemetry 是参考实现、适配成本低 |
