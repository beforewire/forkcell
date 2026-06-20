# ForkCell 中文说明

[English](README.md) | 中文

ForkCell 是面向 AI Agent 的 governed execution cell 控制层：它把一次本地 agent 执行组织成可回滚、可审计、可复查的事务。

> checkpoint -> governed run -> receipt -> accept / restore / fork

当前仓库是 `v0.1.0-preview` source preview（Python package version 为 `0.1.0a1`）。这个 preview 分支刻意保持精简：只包含 ForkCell 控制面、固定版本的 governed-runtime submodule、runtime patch provenance，以及足够让用户理解和跑通的文档/脚本。

## 为什么需要 ForkCell

AI Agent 会修改代码仓库、安装依赖、调用 API、接触凭证。普通 sandbox 可以隔离进程，普通 snapshot 可以回滚文件，但团队还需要回答：

- Agent 从哪个 checkpoint 开始？
- 本次执行受哪个 runtime policy 约束？
- 是否发生了 egress / L7 policy 事件？
- 失败后是 accept、restore，还是 fork？
- Reviewer 能否看一份稳定 receipt，而不是翻原始日志？

ForkCell 的目标是把一次高风险 agent command 变成一个可审计的 transaction。

## ForkCell 负责什么

ForkCell 是 transaction/control plane：

- 创建 workspace filesystem checkpoint；
- 通过 governed runtime integration 执行命令；
- 把 policy revision、checkpoint identity、workspace config、command result 绑定到 receipt；
- 把 accept / restore / fork 决策记录为一等 artifact；
- 通过 native overlay backend 做快速 metadata generation switch；
- 明确记录 fallback / degraded backend 的选择原因。

Runtime enforcement 由配置的 governed runtime 负责。当前 preview 使用 pinned OpenShell runtime fork，具体见 `Runtime Integration`。

ForkCell **不** 在核心层实现退款金额、理赔资格等业务语义 policy。这类规则应该放在外部 application / PDP / tool gateway。ForkCell 聚焦 runtime capability policy 和 transaction receipt。

## 仓库结构

```text
forkcell/
  forkcell/                  # Python CLI/API 和 checkpoint providers
  scripts/                   # preview build / gateway / smoke scripts
  patches/                   # runtime patch review/provenance artifacts
  docs/                      # architecture 和 preview docs
  upstream/openshell         # submodule: 当前固定的 runtime fork
```

OpenShell patch 已经应用在固定版本的 `beforewire/openshell` submodule 中。`patches/` 下的 patch 文件用于 review / upstreaming provenance，不是正常 build 时动态 apply 的依赖。

## Runtime Integration

当前 preview runtime integration 使用固定的 OpenShell fork：

```text
repo:    https://github.com/beforewire/openshell
branch:  forkcell-workspace-substrate
tag:     forkcell-runtime-v0.1.3-preview
commit:  393c25a86d9128ff5e38ecf537809efe58470266
```

这个 runtime fork 的 patch 范围很窄：

- Docker driver 接受 typed `docker.workspace` / `forkcell_overlay` contract；
- workspace backing volume 挂载到 runtime 私有路径；
- supervisor 在 privilege drop / hardening 之前准备并 chown overlay runtime directories；
- runtime policy、egress、credential、OCSF 路径保持不变。

在当前 preview 中，OpenShell 提供 runtime enforcement：sandbox lifecycle、process/filesystem policy、egress/L7 policy、credential/provider path、OCSF/log events。

## 安装路径

ForkCell 当前有两种 preview 使用路径：

- **PyPI package path**：只安装 Python CLI/API，适合先跑本地 overlay rollback demo。
- **Source runtime path**：clone GitHub 仓库并初始化 submodule，适合跑完整 patched OpenShell governed-runtime demo。

PyPI package 不包含 `scripts/`、`patches/`、`upstream/openshell` submodule；这些内容只在 GitHub source repository 中提供。

## PyPI Quickstart

如果你通过 PyPI 安装，使用这条路径：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install forkcell==0.1.0a1

mkdir -p workspace
printf 'hello\n' > workspace/hello.txt

forkcell overlay init demo --from workspace
forkcell overlay run --checkpoint-before --restore-on-fail demo -- \
  sh -lc 'echo changed > hello.txt; exit 7'

cat workspace/hello.txt
forkcell receipt show --cell demo --latest --format md
```

这个命令会故意 `exit 7`。成功标准不是 command exit 0，而是：

- receipt 中出现 `Decision: restored`；
- 最终 `cat workspace/hello.txt` 输出 `hello`。

这条路径使用本地 overlay rollback backend，可以展示 checkpoint、restore、receipt 语义，但不会启动 patched OpenShell runtime，也不会启用 OpenShell network/credential policy enforcement。

## Source Runtime Quickstart

如果要运行完整 ForkCell + patched OpenShell runtime preview，使用这条路径。

前置条件：

- macOS 或 Linux host，Docker 可用；
- Python 3.11+；
- Rust/Cargo，用于 build OpenShell CLI/gateway；
- 能访问 public `beforewire/openshell` submodule。

Clone：

```bash
git clone --recurse-submodules https://github.com/beforewire/forkcell.git
cd forkcell
```

或者 clone 后初始化 submodule：

```bash
git submodule update --init --recursive
```

本地安装 ForkCell：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

运行轻量 smoke：

```bash
./scripts/validate_public_smoke.sh
```

Build 固定 runtime：

```bash
./scripts/build_patched_openshell_runtime.sh
python3 -m forkcell.cli runtime install --from upstream/openshell
```

启动本地 patched runtime gateway：

```bash
./scripts/start_patched_openshell_gateway.sh
export FORKCELL_OPENSHELL_BIN="$PWD/.forkcell/runtime/native-overlay/bin/openshell"
export OPENSHELL_GATEWAY_ENDPOINT="http://127.0.0.1:17671"
```

创建 native cell，并运行 restore-on-fail demo：

```bash
mkdir -p /tmp/forkcell-demo
printf 'hello\n' >/tmp/forkcell-demo/hello.txt

python3 -m forkcell.cli native init demo --from /tmp/forkcell-demo
python3 -m forkcell.cli native run --checkpoint-before --restore-on-fail demo -- \
  sh -lc 'echo changed > hello.txt; exit 7'
python3 -m forkcell.cli receipt show --cell demo --latest --format md
cat /tmp/forkcell-demo/hello.txt
```

这个命令会故意在 sandbox 内 `exit 7`。成功标准不是 command exit 0，而是：

- receipt 中出现 `Decision: restored`；
- 最终 `cat /tmp/forkcell-demo/hello.txt` 输出 `hello`。

停止 gateway：

```bash
./scripts/stop_patched_openshell_gateway.sh
```

## 当前性能指标

当前 preview validation：

- native overlay `restore_sync_ms`: `0ms`，覆盖 small / medium / pruned workspaces；这里的含义是同步 generation switch sub-ms / 四舍五入为 0；
- native overlay correctness matrix: `7/7` cases passed；
- native policy smoke: deny host、allow GET、L7 deny passed；
- runtime packaging 和 CI-style gate passed；
- runtime sandbox lifecycle 仍然是数百毫秒量级，所以只能把同步 restore substrate 描述为 sub-ms / `0ms`，不能说完整 sandbox lifecycle 是 0ms。

README 路径下的 tiny workspace demo 曾产生：

- checkpoint duration: `0ms`；
- restore duration: `0ms`；
- `restore_sync_ms`: `0ms`；
- `total_restore_path_ms`: `726ms`，包含 runtime sandbox delete/lifecycle 和 log collection。

更多见 `docs/evidence-summary.md`。

## Backends

- `native-overlay`: production fast path，需要 patched governed runtime；
- `layer-clone`: compatible fallback，restore metadata-only，但 run-layer preparation 会 copy checkpoint tree；
- `volume-delta`: governed Docker volume workspace，使用 CAS/delta restore；
- `local-overlay`: 本地开发降级路径，policy/isolation 降级。

当前 preview 中，`native-overlay`、`layer-clone`、`volume-delta` 都由 OpenShell 支撑。Backend 名称表达 ForkCell restore strategy；runtime integration 表达底层 sandbox/policy engine。

## 非目标

- 不做 memory/process checkpoint；
- 不提供 VM/MicroVM/KVM isolation layer；
- 不在核心层实现 business-semantic policy evaluator；
- 不替代 OpenShell policy/egress enforcement；
- 不宣称 pure macOS/Windows native isolation；
- 不宣称完整 sandbox lifecycle 是 `0ms`。

## 文档

- `docs/architecture.md`：产品边界和 control-plane architecture；
- `docs/openshell-native-fast-substrate.md`：OpenShell workspace substrate design；
- `docs/testing-plan.md`：preview smoke 和 integration validation plan；
- `docs/benchmark-matrix.md`：benchmark matrix 和 performance breakdown guide；
- `docs/rust-core-boundary.md`：Rust core decision boundary；
- `docs/evidence-summary.md`：sanitized validation summary。

## About BeforeWire

ForkCell 是 BeforeWire agent-trust infrastructure 方向的一部分：让 agent execution 可复查、可回滚、可被 policy 约束，而不是一开始就假设本地开发必须使用完整 cloud MicroVM 产品。

## 状态

`v0.1.0-preview` / `0.1.0a1` 是实验性版本。这个 preview 用于展示产品边界，以及 checkpoint / restore / receipt 这条 working path。后续会继续补充更完整的运行时矩阵、验证门禁和示例场景。
