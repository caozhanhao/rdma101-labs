# 实验零：配置开发环境

**实验目标**　准备一台能跑 RDMA 的 Linux：装好 Soft-RoCE（RXE）设备，构建所选语言的 Transfer Engine 框架，通过实验零的环境与接口验收。本实验不涉及 Engine 的实现。

C、C++、Rust 三种 Engine 都在 Linux 内编译、调试和测试；测试环境需要可用的 RDMA 设备，RXE 即可。macOS/Windows 仅作为 Linux VM 的宿主，不在宿主上构建 Engine。

下面是一套推荐配置，方便统一环境、提供配置指导和排障，并不是实验的硬性要求。你也可以用自己的发行版、虚拟机工具或已有的 Linux 环境，完成后面的构建和 RDMA 通信检查即可。

| 宿主系统    | 推荐的入口                              |
| ----------- | --------------------------------------- |
| Linux       | Ubuntu 24.04；需要隔离时用 Multipass VM |
| macOS       | Multipass，使用默认 QEMU 后端           |
| Windows x64 | VirtualBox，安装官方 Ubuntu Server ISO  |

推荐配置如下。VM 资源是起始建议，后面可以按需调整，不代表最低要求：

| 项目    | 配置                                                         |
| ------- | ------------------------------------------------------------ |
| 系统    | Ubuntu Server 24.04 LTS，无需图形界面                        |
| 内核    | 优先采用 GA 6.8 系列的 generic 内核                          |
| 架构    | amd64 或 arm64，在 Linux 内原生编译                          |
| VM 资源 | 4 vCPU、4 GiB 内存、32 GiB 磁盘                              |
| 工具链  | Ubuntu 的 C/C++ 工具链与 Python 3.12；uv 0.12.5；Rust 1.98.1 |

实验只需要一台 Linux：RXE 在软件里实现 RDMA verbs，不需要 RDMA 网卡；两端进程在一台 VM，通过虚拟以太网接口通信。

## 一、获得 Ubuntu

按宿主系统选一条路径：

- Linux 宿主：直接用现有的 Ubuntu 24.04，或按下面安装 Multipass 起一台 VM；
- macOS：安装 Multipass，后端用默认的 QEMU；
- Windows：用 VirtualBox 和官方 ISO 安装一台 VM。

### 在 Linux 宿主机上

已经有 Ubuntu 24.04，希望在宿主机直接开发的话，可以直接进入第二节。

如果不希望在宿主机直接开发，也可按下一节里 multipass 的步骤创建并进入实例。

### 在 macOS 上

从 [Multipass 官方安装指南](https://canonical.com/multipass/docs/latest/how-to-guides/install-multipass/)下载并安装 macOS 的 `.pkg`。

安装后在 macOS 终端执行：

```bash
multipass version
multipass get local.driver
```

新安装的默认后端应为 `qemu`。这时候就可以创建实例了，实例显式指定 24.04 （详细参数见 [Multipass launch](https://canonical.com/multipass/docs/latest/reference/command-line-interface/launch/)）：

```bash
multipass launch 24.04 --name rdma101 --cpus 4 --memory 4G --disk 32G
```

实例创建好后，接着执行：

```bash
multipass info rdma101
multipass shell rdma101
```

最后一条命令以 `ubuntu` 用户进入 VM。在 VM 中执行 `exit` 可返回宿主机终端，VM 会继续运行。

### 在 Windows 上：VirtualBox 与 Ubuntu ISO

这条路径适用于 Windows x64 的家庭版、专业版等版本。由于相关资料网上较为丰富，这里只简要介绍一下基本的流程：

1. 安装 [VirtualBox Windows 版](https://www.virtualbox.org/wiki/Downloads)，下载 [Ubuntu 24.04 官方镜像](https://releases.ubuntu.com/24.04/)中的 **Server install image for 64-bit PC (AMD64)**。下载慢时可以从[中科大 Ubuntu Releases 镜像站](https://mirrors.ustc.edu.cn/ubuntu-releases/24.04/)获取同一版本的 Server AMD64 ISO。
2. 新建名为 `rdma101` 的虚拟机，选择下载的 ISO，关闭自动无人值守安装，设置 4 个 CPU、4 GiB 内存、32 GiB 虚拟磁盘。网络使用默认的 NAT。
3. 启动安装程序，使用 DHCP 网络和虚拟磁盘；选择普通 Ubuntu Server，不安装桌面。创建普通用户，这里以 `lab` 为例；勾选安装 OpenSSH server。
4. 安装完成后移除虚拟光驱中的 ISO，重启并登录。
5. 在虚拟机的“设置 → 网络 → NAT 网卡 → 端口转发”中增加一条 TCP 规则：主机 IP 为 `127.0.0.1`，主机端口为 `2222`，子系统 IP 留空，子系统端口为 `22`。

安装界面可参考 [VirtualBox 创建 VM](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/create-vm.html)，端口转发见 [VirtualBox NAT 文档](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/networkingdetails.html)。

在 Windows PowerShell 中连接；用户名需与安装时一致：

```powershell
ssh -p 2222 lab@127.0.0.1
```

此后即进入这台 Ubuntu，后面的命令都在其中执行。如果这步找不到 `ssh`，需要在 Windows“可选功能”里装 OpenSSH 客户端。

## 二、安装 Linux 开发依赖

**从这里开始，除标注为宿主机操作的部分外，命令都在 Linux 的 Bash 中执行。** 先确认系统、运行内核和架构：

```bash
cat /etc/os-release
uname -r
dpkg --print-architecture
id
```

架构应为 `amd64` 或 `arm64`，当前用户不应是 root。

### 安装软件包

下面是 `apt update` 与依赖安装：

```bash
sudo apt update
sudo apt install -y \
  build-essential cmake pkg-config clang libclang-dev gdb \
  git curl ca-certificates python3 python3-dev python3-venv libffi-dev \
  libibverbs-dev ibverbs-providers ibverbs-utils rdma-core perftest \
  iproute2 kmod openssh-server
```

其中 `libibverbs-dev` 提供 verbs 头文件和链接库，`ibverbs-providers` 提供用户态 provider，`ibverbs-utils` 与 `perftest` 提供设备和通信诊断工具。

### 获取仓库与 Python 环境

下面假定仓库位于 `~/rdma101-labs`，新环境执行：

```bash
# 如果你来自光点计划 (https://csinfra.cn)，请 clone 活动页面为你创建的专属仓库，否则后续将无法通过 git push 提交代码。
git clone https://github.com/caozhanhao/rdma101-labs.git "$HOME/rdma101-labs"
cd "$HOME/rdma101-labs"
```

在 Linux 的普通账户下安装 uv：

```bash
curl -LsSf https://astral.sh/uv/0.12.5/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
```

接下来配置 uv 下载 Python 依赖时使用的源。在实验用的 Linux 中编辑 `~/.config/uv/uv.toml`，目录或文件不存在就创建；已有默认索引时，修改对应条目：

```toml
[[index]]
url = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
default = true
```

这是当前 Linux 用户的持久配置，后续 `uv sync` 和 `uv run` 会自动读取。镜像站说明见[清华 PyPI 源](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)，配置文件说明见 [uv 官方文档](https://docs.astral.sh/uv/concepts/configuration-files/)。

然后在仓库目录安装项目依赖：

```bash
uv sync
```

### Rust 工具链（可选）

如果选择 C 或 C++ 实现的话可以跳过这一小节。Rust 有两处下载来源：rustup 下载编译器和工具链组件，Cargo 下载项目依赖，需要分别配置。

**rustup 镜像。** 国内网络下可以使用 [RsProxy](https://rsproxy.cn/)。先在 Linux 当前终端执行下面两行，同时把它们加入 `~/.bashrc`，供以后安装或更新工具链使用：

```bash
export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
```

Linux 内还没装 rustup 时，用同一镜像站的安装脚本：

```bash
curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | \
  sh -s -- -y --profile minimal --default-toolchain none
source "$HOME/.cargo/env"
```

**Cargo 依赖镜像。** 将下面的配置合并到 `~/.cargo/config.toml`：

```toml
[source.crates-io]
replace-with = "rsproxy-sparse"

[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"
```

配置依据见 [RsProxy 使用说明](https://rsproxy.cn/)和 [Cargo source replacement](https://doc.rust-lang.org/cargo/reference/source-replacement.html)。

在仓库目录选择本次使用的工具链：

```bash
cd "$HOME/rdma101-labs"
rustup toolchain install 1.98.1 --profile minimal --component rustfmt --component rust-analyzer
rustup override set 1.98.1
rustc --version
cargo --version
```

`override` 只对该目录及其子目录生效，见 [rustup 目录工具链选择](https://rust-lang.github.io/rustup/overrides.html)。

## 三、配置 RXE

### 安装并加载 RXE 内核模块

`rdma_rxe` 是 Soft-RoCE 的内核驱动。在前面的 Ubuntu 24.04 环境中，直接执行以下命令：

```bash
sudo apt update
sudo apt install -y "linux-modules-extra-$(uname -r)"
sudo modprobe rdma_rxe
sudo modprobe ib_uverbs
modinfo -n rdma_rxe
```

`modprobe` 成功时没有输出；最后一条命令应显示 `rdma_rxe` 模块文件的路径。补装并加载当前内核的模块通常不需要重启。

### 选择以太网接口并创建设备

本文以 IPv4 演示接口和 GID 的选择；[RXE 也支持 IPv6](https://man7.org/linux/man-pages/man7/rxe.7.html)。

```bash
ip -br -4 address
rdma link show
```

第一条命令会显示接口名称、状态和 IPv4 地址。在 VM 里选一个状态为 `UP`、具有 IPv4 地址的以太网接口，例如 `enp0s1`、`enp0s3` 或 `eth0`，不要选 `lo`。下面的接口名按实际输出修改：

```bash
export R101_NETDEV=enp0s1
export R101_DEVICE=rxe0
export R101_PORT=1
```

`rdma link show` 里还没有这个设备时，创建它：

```bash
sudo rdma link add "$R101_DEVICE" type rxe netdev "$R101_NETDEV"
ibv_devices
ibv_devinfo -d "$R101_DEVICE" -i "$R101_PORT"
```

预期 `ibv_devices` 里出现所选设备，端口为 `PORT_ACTIVE`，链路类型为 `Ethernet`。

### 选择 GID index

回到仓库目录看诊断结果：

```bash
cd "$HOME/rdma101-labs"
uv run --locked python scripts/doctor.py
```

按本文的 IPv4 配置，在所选设备的这个端口下，挑一个同时满足以下条件的 GID：

- `netdev` 是刚才选定的以太网接口；
- 类型为 `RoCE v2`；
- 地址对应该接口的 IPv4 地址。IPv4-mapped GID 的形式为 `::ffff:a.b.c.d`，这里也可能显示为完整十六进制。

例如接口 IPv4 是 `192.168.252.2` 时，下面这一项对应 index 1：

```text
GID 1: 0000:0000:0000:0000:0000:ffff:c0a8:fc02; RoCE v2; netdev=enp0s1
```

下面以 index 1 为例；如果实际输出中匹配的 index 不同，修改为对应值，再保存配置：

```bash
export R101_GID_INDEX=1
mkdir -p "$HOME/.config/rdma101"
cat > "$HOME/.config/rdma101/env" <<EOF
export R101_NETDEV=$R101_NETDEV
export R101_DEVICE=$R101_DEVICE
export R101_PORT=$R101_PORT
export R101_GID_INDEX=$R101_GID_INDEX
EOF
```

以后每开一个新 Linux 终端，先执行 `source "$HOME/.config/rdma101/env"`。接口、IP 或设备变了，要重新选择并更新这个文件。

## 四、独立验证 RDMA 环境

仓库自带环境检查工具，入口是 `scripts/doctor.py`。加上 `--check` 时，它会构建并运行独立的 verbs 程序 [`scripts/rdma_check.cpp`](../scripts/rdma_check.cpp)，检查设备、资源访问和真实通信。

在 Linux 普通账户下执行：

```bash
cd "$HOME/rdma101-labs"
source "$HOME/.config/rdma101/env"
uv run --locked python scripts/doctor.py --check
```

`doctor.py` 默认读取刚才加载的 `R101_DEVICE`、`R101_PORT`、`R101_GID_INDEX`，也可以用 `--device`、`--port`、`--gid-index` 覆盖。

程序自动启动发送方和接收方，依次完成：

1. 两端分别打开所选设备，确认端口为 ACTIVE、GID 有效，创建各自的 PD、MR、CQ 和 RC QP。
2. 交换连接与内存信息，将 QP 依次转入 INIT、RTR、RTS，并确认双方就绪。
3. 发送方提交一次 4 KiB RDMA WRITE，检查 CQE 状态；接收方逐字节校验内容，确认前后各 64 字节保护区没有被改写。
4. 双方释放资源并退出；任一阶段失败都返回非零退出码。

日志带有 `sender` / `receiver` 和阶段名称，成功时末尾会出现：

```text
[PASS] Resource access, RC connection and 4096-byte WRITE with data/guard checks.
Other RDMA operations, cross-host networking and performance: NOT CHECKED.
```

**这一步只检查基础 WRITE 路径。** SEND/RECV、WRITE_WITH_IMM、READ、Atomic 和 Inline 尚未验证。

### 交叉检查（可选）

这里还可以用 `ib_write_bw` 做一次 RDMA WRITE 带宽测试。这一步需要两个终端（记得 `source "$HOME/.config/rdma101/env"`），终端 A：

```bash
timeout -k 5s 120s ib_write_bw -d "$R101_DEVICE" -i "$R101_PORT" \
  -x "$R101_GID_INDEX" -p 18516 -s 4096 -n 100 -t 16 -Q 1
```

终端 B 使用相同参数并追加服务器地址：

```bash
timeout -k 5s 120s ib_write_bw -d "$R101_DEVICE" -i "$R101_PORT" \
  -x "$R101_GID_INDEX" -p 18516 -s 4096 -n 100 -t 16 -Q 1 127.0.0.1
```

参数含义见 [ib_write_bw 手册](https://manpages.ubuntu.com/manpages/noble/man1/ib_write_bw.1.html)。

## 五、构建 Engine

回到实验仓库，加载已保存的设备配置：

```bash
cd "$HOME/rdma101-labs"
source "$HOME/.config/rdma101/env"
```

选一种语言，增量构建所选实现：

```bash
uv run --locked python scripts/build.py cpp
```

语言参数支持 `c`、`cpp`、`rust`，构建产物分别是 `build/c/librdma101_c.so`、`build/cpp/librdma101_cpp.so` 和 `build/rust/debug/librdma101_rust.so`。尝试构建成功即可。

## 六、日常开发

### VM 管理

Multipass 在宿主执行 `multipass stop rdma101` 停机；重新开始时：

```text
multipass start rdma101
multipass info rdma101
multipass shell rdma101
```

VirtualBox 用 Linux 正常关机，再从 VirtualBox 启动。宿主网络变化之后需要先确认 SSH 地址，如果 VM 的 IP 变了就同步更新宿主的 SSH 配置。

### 恢复 RXE

手动创建的 RXE 设备不会在 VM 重启后自动恢复。重新进入 Linux 后，以普通用户执行：

```bash
cd "$HOME/rdma101-labs"
bash scripts/restore-rxe.sh
```

脚本读取 `~/.config/rdma101/env`，加载内核模块，仅在设备不存在时创建 RXE，检查接口绑定后运行双进程 WRITE。只有加载模块和创建设备使用 `sudo`，通信检查仍以当前用户运行。

WRITE 检查通过后即可继续实验。如果网卡名或 IP 地址发生变化，需要按第三节重新选择网卡和 GID，更新配置后再执行。恢复脚本不会改变当前终端的环境变量，运行测试前仍需要加载配置。

### 增量构建与单用例测试

每开一个新终端，先加载设备配置、进入仓库：

```bash
source "$HOME/.config/rdma101/env"
cd "$HOME/rdma101-labs"
```

修改代码后，用同一条命令完成增量构建与测试。下面以 C++ 为例，选择 C 或 Rust 时把 `cpp` 换成 `c` 或 `rust`。实现做到哪一步，就可以把测试粒度从单个用例逐步扩大：

```bash
# 单个 WRITE 用例
uv run --locked python scripts/test.py cpp -m lab1 -k write_block -vv

# 当前实验，例如实验三
uv run --locked python scripts/test.py cpp -m lab3 -v

# 已完成实验的累积回归，例如实验一至三
uv run --locked python scripts/test.py cpp -m 'lab1 or lab2 or lab3' -v
```

语言之后的参数直接传给 pytest，支持指定文件、用例名和 `-m`、`-k` 等筛选条件。脚本自动启用通信用例；设备参数默认取环境变量，也可以显式传 `--device`、`--port`、`--gid-index`。

测试驱动通过 Python `multiprocessing` 的 `spawn` 模式启动各参与进程；失败、超时或 Ctrl-C 后会清理本轮的子进程。失败时终端显示各进程日志的末尾，完整日志和进程信息保存在输出提示的 `build/logs/r101-*` 目录；成功的运行不保留日志目录。

### 使用 VS Code Remote-SSH

编辑器跑在宿主上，仓库、语言服务和终端都在 Linux 里。宿主装 VS Code 和 Remote-SSH 扩展即可，工作方式见 [VS Code Remote-SSH](https://code.visualstudio.com/docs/remote/ssh)。当然也可以继续用 Linux 终端加其他编辑器。

**Multipass 的 SSH 密钥。** 在宿主生成一对专用密钥，私钥留在宿主，只把公钥传进 VM。

macOS/Linux 宿主：

```bash
mkdir -p "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/rdma101"
multipass transfer "$HOME/.ssh/rdma101.pub" rdma101:/home/ubuntu/rdma101.pub
```

用 `multipass shell rdma101` 进入 Linux，追加公钥：

```bash
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
grep -qxF "$(cat "$HOME/rdma101.pub")" "$HOME/.ssh/authorized_keys" || \
  cat "$HOME/rdma101.pub" >> "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
```

在宿主执行 `multipass info rdma101` 看 VM 地址，然后把下面这段加到**宿主**的 `~/.ssh/config` 里。`HostName` 换成实际地址：

```sshconfig
Host rdma101
    HostName 192.168.252.2
    User ubuntu
    IdentityFile ~/.ssh/rdma101
    IdentitiesOnly yes
```

**Windows 上的 VirtualBox 用户**可以先用安装时设的密码登录，把下面的 SSH 条目加到宿主的 `%USERPROFILE%\.ssh\config` 里，用户名按实际改：

```sshconfig
Host rdma101
    HostName 127.0.0.1
    Port 2222
    User lab
```

**Linux 真机用户**直接在本机打开仓库就行；远程 Linux 用实际主机地址和账户配 SSH。

先在宿主确认 `ssh rdma101` 能连上，再在 VS Code 里执行 “Remote-SSH: Connect to Host”，选 `rdma101`，打开 Linux 内的仓库。C/C++ 或 Rust 扩展装到远程环境。首次连接要下载 VS Code Server 和扩展，尽量在有网络时做。

## 七、验收实验零与环境记录

构建完成后运行实验零的验收。三种实现共用 [`test_00_environment.py`](../tests/engine/test_00_environment.py)：它会再跑一遍独立的双进程 WRITE 环境检查，并检查所选动态库能否加载、导出符号与接口边界是否正确。环境检查仍用仓库自带的 verbs 程序，不需要先实现后续实验的 Engine 数据路径。失败时 pytest 会显示对应检查的日志。

选一种语言运行（也可以三种都跑）：

```bash
uv run --locked python scripts/test.py c   -m lab0 -v
uv run --locked python scripts/test.py cpp -m lab0 -v
uv run --locked python scripts/test.py rust -m lab0 -v
```

以下检查都过了，就可以进入[实验一](01-write.md)：

- 设备名、端口和 GID index 已确定并保存。
- 所选语言的 `-m lab0` 验收通过：独立 WRITE 的数据与保护区校验正确，动态库加载、导出符号与接口边界检查通过。
- 能重新连上 Linux，并在其中编辑代码、增量构建和运行单个测试。
- 重启后能恢复设备并重新跑通 WRITE。

另外可以保存一份 Linux 侧环境记录，便于复现和排障：

```bash
cd "$HOME/rdma101-labs"
uv run --locked python scripts/doctor.py --report build/environment.txt
```

报告包含系统、内核、工具链、所选设备参数及设备诊断输出。缺少工具或设备时也会保存已收集的信息；报告本身不执行通信检查。

## 八、环境常见故障

| 现象                          | 先检查什么                                                                       |
| ----------------------------- | -------------------------------------------------------------------------------- |
| VM 创建失败                   | 宿主版本、虚拟化是否启用、所选后端、可用内存与磁盘；查看虚拟化工具自身的错误     |
| SSH 连不上                    | VM 是否运行、实际 IP、OpenSSH 服务；VirtualBox 的 NAT 端口转发是否正确           |
| 端口不是 ACTIVE               | RXE 绑定的 netdev、接口状态和 IP 地址                                            |
| 打开设备失败                  | 实际运行用户与设备节点权限；不要先改为 root 运行                                 |
| `ibv_reg_mr` 返回 ENOMEM      | 用 `ulimit -S -l` 查看当前 memlock 额度（KiB），结合注册内存大小排查             |
| 检查工具编译失败              | `c++` 与 `libibverbs-dev` 是否安装；查看编译器的原始错误                         |
| 检查工具超时或 CQE 报重试耗尽 | 两端日志的最后阶段、设备与 GID、QP 状态；不要只延长超时掩盖配置错误              |
| 动态库不存在或加载失败        | 是否构建了所选语言、`--library` 路径、当前系统架构、libibverbs/provider 是否齐全 |
