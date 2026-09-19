# RDMA101 Labs - Bake Your Own Mooncake

欢迎来到 RDMA101 Labs。

在本实验中，你将从零开始亲手构建一个 Mini Transfer Engine。本实验以 [Feng Ren](https://renfeng.org) 编写的 [RDMA101](https://renfeng.org/RDMA101/) 为基础，融合了 [Mooncake](https://github.com/kvcache-ai/Mooncake) 的实际工程经验，旨在帮助同学们系统了解 RDMA 编程范式，并理解现代高性能传输系统的设计与实现。

## 参与实验

- **前置要求**：熟悉 Linux，了解指针、内存与进程模型，能够调试系统代码。不要求具备 RDMA 基础。
- **语言选择**：支持 C、C++ 或 Rust。三种语言共享同一套 C ABI 约定、学习目标与测试用例。
- **运行环境**：Linux 环境，基于 Soft-RoCE（RXE）即可，无需物理 RDMA 网卡。Windows 和 macOS 用户可通过虚拟机参与，详见 [实验零 - 环境配置](docs/00-environment.md)。

## Engine 设计

本实验设计的 Transfer Engine 的接口如下（省略公共 `r101_` 前缀）：

| 操作         | 接口                                                     |
| ------------ | -------------------------------------------------------- |
| 创建与销毁   | `create_engine`、`destroy_engine`                        |
| 本地内存注册 | `register_local_memory`、`deregister_local_memory`       |
| 对端连接     | `create_peer`、`connect_peer`、`destroy_peer`            |
| 远端内存描述 | `import_remote_memory`、`remove_remote_memory`           |
| 传输与完成   | `submit_transfer`、`get_transfer_statuses`、`free_batch` |

如果你了解 Mooncake Transfer Engine，可以将其视为一个面向 RDMA 教学的精简版：

- **保留**了核心的内存注册、批量异步提交与状态查询机制；
- **简化**了引擎职责，将元数据交换交由应用层完成、仅聚焦 Host Memory 上的 RDMA；
- **剥离**了多传输后端的选择与拓扑感知的设备选择与调度。

详细接口约定与规范请参阅公共头文件 [`transfer-engine/transfer_engine.h`](transfer-engine/transfer_engine.h)。

## 实验路线

在完成环境准备后，后续八个实验沿一条由浅入深的主线展开：从最基础的同步 WRITE 出发，逐步引入异步机制、内存切片与发送窗口，最终补齐消息通知、按需拉取与远端原子操作。

| 实验   | 内容                                          | 讲义                                        |
| ------ | --------------------------------------------- | ------------------------------------------- |
| 实验零 | 配置环境：Soft-RoCE（RXE）、双进程 WRITE 检查 | [00-environment.md](docs/00-environment.md) |
| 实验一 | 同步 WRITE                                    | [01-write.md](docs/01-write.md)             |
| 实验二 | 异步提交与非阻塞查询                          | [02-async.md](docs/02-async.md)             |
| 实验三 | 多请求与切片                                  | [03-slices.md](docs/03-slices.md)           |
| 实验四 | 发送窗口                                      | [04-window.md](docs/04-window.md)           |
| 实验五 | 批量投递与选择性信号                          | [05-batching.md](docs/05-batching.md)       |
| 实验六 | 消息、通知与 Inline（SEND/RECV/WRITE_IMM）    | [06-messages.md](docs/06-messages.md)       |
| 实验七 | 按需拉取（RDMA READ）                         | [07-read.md](docs/07-read.md)               |
| 实验八 | 原子更新（COMPARE_SWAP / FETCH_ADD）          | [08-atomic.md](docs/08-atomic.md)           |

## 致谢

感谢以下开发者、开源项目与社区为本实验提供的启发与支持：

- **[Feng Ren](https://renfeng.org)**：[RDMA101](https://github.com/alogfans/RDMA101) 作者，为本实验提供了清晰扎实的教学脉络与底层实验参考。
- **[Mooncake](https://github.com/kvcache-ai/Mooncake)**：其 Transfer Engine 为本实验提供了直接设计参考与工程启发。
- **[光点计划](https://csinfra.cn)**：电子科技大学 Infra 学习社区，为本实验的早期落地提供了社区支持。
