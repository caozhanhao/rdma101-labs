# 实验一：同步 WRITE

**实验目标**　实现 Transfer Engine 的完整生命周期，使应用能够把 4 KiB 数据从一个进程的本地内存写入另一个进程的指定地址，并在提交返回后查询到稳定的任务结果。

**教材**　[2.1 RDMA WRITE 范例解读](https://renfeng.org/RDMA101/02-programming-model/01-first-rdma-program/)、[2.2 Context 与 Protection Domain](https://renfeng.org/RDMA101/02-programming-model/02-context-and-pd/)、[2.3 Memory Region](https://renfeng.org/RDMA101/02-programming-model/03-mr/)、[2.4 Queue Pair](https://renfeng.org/RDMA101/02-programming-model/04-qp/)、[2.5 Completion Queue](https://renfeng.org/RDMA101/02-programming-model/05-cq/)、[2.7 RDMA WRITE](https://renfeng.org/RDMA101/02-programming-model/07-rdma-write/)

**前置**　完成[实验零](00-environment.md)：Linux 内 Soft-RoCE（RXE）可用，所选语言能构建并通过公共接口检查。

## 背景

本实验实现一个 Transfer Engine。它是应用与 verbs 之间的一层抽象：应用把自己要搬运的数据交给它，由它在进程之间完成搬运。实验一只处理同步的单边 WRITE，后续实验逐步扩展它的能力。

### 一次 RDMA WRITE 需要什么

一次 WRITE 由发起方把本地内存的一段数据写进对端内存的指定位置。虽然数据只在一个方向上流动，但两端都需要各做准备。

**两端各自准备本地环境**（教材 2.2、2.3）：

- 打开设备、创建 PD 与 CQ；
- 注册要参与传输的缓冲区，得到 `lkey`；对端要访问的那块缓冲区还需要把 `rkey` 交给对端。

**两端各自建立连接**（教材 2.4）：

- 创建 QP，并初始化到可发送、可接收的状态；
- 这一步需要知道对端的 QPN、GID 等参数，它们只能由对端提供，因此交换必须双向完成。这次交换由测试框架负责（见下文"谁负责什么"），Engine 只负责导出自己的一半、消费对端的一半。

**发起方提交并等待完成**（教材 2.7、2.5）：

- 提交的描述包含两段范围：本地一段（地址与 `lkey`）和远端一段（对端地址与 `rkey`）；
- 设备把数据写入对端内存，接收方的应用不参与这次搬运；
- 发起方从 CQ 取得完成通知，此后本地缓冲区才能复用。

教材 [2.1](https://renfeng.org/RDMA101/02-programming-model/01-first-rdma-program/) 的 WRITE 示例按这个顺序调用 verbs，展示了每一步怎么做。程序只需要搬一次数据时，这样写是最直接的做法。

### 从一次传输到一个引擎

持续搬运数据时——一个进程不断产生数据块，另一个进程准备好接收内存，数据块被逐一搬过去——上面这些工作的性质分成两类：大部分只需要做一次（设备与 PD、注册的缓冲区、与某个对端的连接），此后长期有效；只有与数据内容相关的那部分需要反复执行（提交哪两段范围、等待完成、复用缓冲区）。

Transfer Engine 就是这整个流程的抽象，而不仅仅是把其中某一步封装成一个函数：

- **资源与连接成为引擎内部的状态。** 应用不再持有 PD、QP、CQ，为每块缓冲区注册 MR、为每个对端创建并连接一次，此后的传输复用这些资源。注册区与连接可以单独回收，也可以保留到引擎销毁。
- **连接由双方各自建立。** 元信息是两端各自产生、必须交换的数据：一方导出自己的一半，导入对端的一半才能完成本地连接。
- **应用声明想做什么，而不是怎么做。** 应用给出的是"哪一段搬到哪一段"的声明式请求；每次投递多少、同时在途多少、一次投递几个 WR，都是引擎自己的决定，应用不参与。
- **引擎不理解数据的含义。** 请求只描述范围与操作；这些数据是什么、何时可以释放，由应用决定。

### 由此得到的接口形状

Transfer Engine 的接口则按上面两类工作划分：资源与连接管理，以及传输的提交、查询与回收。

| 类别       | 应用需要做                          | 接口                           | 引擎在这一次调用中做                                       |
| ---------- | ----------------------------------- | ------------------------------ | ---------------------------------------------------------- |
| 资源与连接 | 填入引擎配置                        | `r101_create_engine`           | 选定并打开设备，创建 PD 与 CQ；失败时回收已创建的资源      |
| 资源与连接 | 提供缓冲区的地址、长度与权限        | `r101_register_local_memory`   | 注册 MR，返回注册 handle 与该 MR 的元数据                  |
| 资源与连接 | 确认本地与远端都不再使用该注册区    | `r101_deregister_local_memory` | 注销本地 MR，不释放应用分配的缓冲区                        |
| 资源与连接 | 取走本端连接元数据，并交给对端      | `r101_create_peer`             | 创建 QP 并置为 INIT，返回 peer handle 与连接元数据         |
| 资源与连接 | 把对端元数据与 peer handle 交给引擎 | `r101_connect_peer`            | 解析并复制需要保留的内容，把 QP 迁移到 RTR、RTS            |
| 资源与连接 | 把需要访问的远端 MR 元数据交给引擎  | `r101_import_remote_memory`    | 保存范围、权限与 rkey，返回本机的导入 handle               |
| 资源与连接 | 不再使用这份远端内存描述            | `r101_remove_remote_memory`    | 移除本机导入的描述，但不注销对端的 MR                      |
| 资源与连接 | 结束该连接                          | `r101_destroy_peer`            | 终止该 peer 的任务并回收连接资源，保留本地 MR 和 batch     |
| 资源与连接 | 确认双方都不再访问注册内存          | `r101_destroy_engine`          | 结束本地设备访问，回收引擎资源                             |
| 传输       | 提交要搬运的本地与远端范围          | `r101_submit_transfer`         | 校验并接受请求，开始传输                                   |
| 传输       | 一次读取整批任务的状态与结果        | `r101_get_transfer_statuses`   | 按提交顺序返回全部任务状态；只读已保存的结果               |
| 传输       | 任务全部终止后释放记录              | `r101_free_batch`              | 释放该批次的任务与完成记录，保留连接与注册区供后续传输复用 |

应用侧的使用形态如下：

```c
/* 设置：建立本地资源、注册内存、与对端建立连接 */
r101_create_engine(&config, &engine);
r101_register_local_memory(engine, address, length, access,
                           &memory, &memory_metadata, &memory_metadata_size);
r101_create_peer(engine, &peer, &connection_metadata, &connection_metadata_size);
/* 应用交换连接元数据 */
r101_connect_peer(engine, peer, remote_connection_metadata, remote_connection_size);
/* 应用交换需要访问的 MR 元数据，并确认双方连接完成 */
r101_import_remote_memory(engine, peer, remote_memory_metadata, remote_memory_size,
                          &remote_memory);

/* 每次传输：描述要搬什么，随后查询结果 */
r101_submit_transfer(engine, requests, count, &batch);
r101_get_transfer_statuses(engine, batch, statuses, count);
r101_free_batch(engine, batch);

/* 结束：回收全部资源 */
r101_destroy_engine(engine);
```

### 谁负责什么

一次传输被拆成三部分，本实验实现前两部分：

| 部分                               | 内容                                                                   | 由谁实现    |
| ---------------------------------- | ---------------------------------------------------------------------- | ----------- |
| RDMA 数据路径                      | 构造并投递 WR、轮询 CQ、按 `wr_id` 记账、推进任务状态                  | 你的 Engine |
| RDMA 控制路径                      | 打开设备、创建 PD / CQ / QP、注册 MR、把 QP 迁移到 RTR / RTS、回收资源 | 你的 Engine |
| 带外（Out-of-Band，OOB）元数据交换 | 把连接与内存元数据交给对端、确认双方就绪、提供便利接口                 | 测试框架    |

由此得到一条贯穿全部实验的约定：`create_peer` 只导出本端连接参数（QPN、PSN、GID 等），`register_local_memory` 只导出本端 MR 描述（范围、权限、rkey）。**你不需要自己实现 OOB 握手、进程发现或元数据交换**：把连接与内存元数据交给对端、以及对端元数据的导入，都由测试框架完成；你的 Engine 只负责导出与消费这些元数据。

## 实验内容

### 范围与边界

**实验边界：** 实验一只要求把一次 WRITE 整理为一个可复用的引擎：资源与连接只建立一次，此后每次传输只描述要搬运的两段范围。可以缩小为只同步处理一个 peer、一个 batch 和其中的一条连续 RDMA WRITE。

本实验不要求异步执行，你可以在 `submit_transfer` 内构造 WR、调用 `ibv_post_send`、循环 `ibv_poll_cq`，按 `wr_id` 定位任务并保存结果，任务进入 COMPLETED 或 FAILED 后再返回非零 batch handle。公共头文件只是描述最终的异步接口，本实验先实现其同步版本，函数与参数保持一致；[实验二](02-async.md) 再把投递与完成处理交给后台线程。构造 WR 时可参考教材示例 [`examples/one_sided_write/one_sided_write.c`](https://github.com/alogfans/RDMA101/blob/main/examples/one_sided_write/one_sided_write.c)。

**不在本实验范围内：** 后台线程与异步提交（实验二）；多请求与切片（实验三）；多个 batch 与发送窗口（实验四）；批量投递与选择性信号（实验五）。SEND、RECV、WRITE_IMM、READ、Atomic 在实验六至八实现，收到时返回 UNIMPLEMENTED 即可。设备或通信错误之后的处理（错误 CQE 的恢复、QP 进入错误状态后的重建）不在本实验要求之内。

### 实现入口

本实验采用同一份 C ABI，提供 C、C++、Rust 三种实现选择：

| 语言 | 实验代码入口           | 与公共接口的关系                                      |
| ---- | ---------------------- | ----------------------------------------------------- |
| C    | `transfer-engine/c`    | 直接实现 `r101_*` 函数，在此定义 `struct r101_engine` |
| C++  | `transfer-engine/cpp`  | 实现 `rdma101::Engine`，由已有的 `ffi.cpp` 适配       |
| Rust | `transfer-engine/rust` | 实现 `Engine`，由已有的 `ffi.rs` 适配                 |

选择一种语言即可，内部结构你可以自行组织。下面的实现要点以 C 符号给出，C++ 与 Rust 对应 `Engine` 的同名方法。此外，后文中的状态码、错误码、标志位与 opcode 名称一律省略公共 `R101_` 前缀（`OK` 即 `R101_OK`，`REMOTE_READ` 即 `R101_REMOTE_READ`，`COMPARE_SWAP` 即 `R101_COMPARE_SWAP`）。

## 实现要点

**1. 创建引擎：资源创建与回收。** C 实现 `r101_create_engine`，C++/Rust 实现 `Engine::create`。校验配置，打开设备、创建 PD 与 CQ。任一步骤失败应当释放已创建的资源。

**2. register_local_memory：校验、注册、导出元数据。** 校验长度大于 0、地址加长度不溢出、注册区不重叠、权限位合法等参数，然后调用 `ibv_reg_mr`。返回注册 handle 和这块 MR 的元数据。元数据的内存需要由 Engine 保存到成功注销或销毁；后续注册不得使旧元数据失效。

**3. create_peer：创建 QP、导出元数据。** 创建 RC QP 并置为 INIT，把连接参数编码为元数据。同样的，元数据由 Engine 持有，在该 peer 或 Engine 销毁前保持只读且地址有效。

**4. connect_peer 与 import_remote_memory：连接并导入远端内存。** 前者解析对端连接元数据，依次迁移本地 QP 到 RTR、RTS；后者解析独立的 MR 元数据，将范围、权限、rkey 与注册身份存入该 peer 的远端表。这两个函数需要的元数据就是对端 create_peer 和 register_local_memory 产生的元数据。

**5. submit_transfer：开始传输。** 校验全部传输请求并开始传输。传输一旦开始，已经发生的写入无法回滚，因此校验与资源准备必须在开始传输之前完成。参数错误返回 INVALID，未连接的 peer 用于非 RECV 操作返回 INVALID_STATE，尚未实现的操作返回 UNIMPLEMENTED；任何失败都不接受任务、不产生 batch。

**6. 查询与回收。** `get_transfer_statuses` 填写调用者提供的状态数组：C 使用 `r101_transfer_status[]`，C++ 使用 `std::span<TransferStatus>`，Rust 使用 `&mut [TransferStatus]`。该函数只读取任务状态，不得投递、轮询或触发调度。`free_batch` 释放 batch 记录，但保留连接与 MR。

**7. 销毁引擎。** 释放 QP、MR、CQ、PD 与 context。C 在 `r101_destroy_engine` 中清理并释放引擎对象，C++ 实现析构函数，Rust 实现 `Drop`。销毁应在设备不再访问应用缓冲区之后进行。

## 验收

实验一的测例会按下面的要求检查你的 Engine 行为：

| 编号 | 场景与输入                                                                                                                                                                                                    | 预期行为                                                                                                                                                                                              |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| L1.1 | 两个进程各自建立完整生命周期：创建引擎、注册带保护区的 4 KiB 缓冲区、创建 peer、交换连接元数据并连接、交换 MR 元数据并导入                                                                                    | 各步返回 OK；注册 handle 与 peer handle 非零；连接元数据与内存元数据均非空。此后新增一个注册区、再创建一个 peer，第一份元数据的内容与地址仍然有效。                                                   |
| L1.2 | 发送方提交一条 4 KiB WRITE，目标是接收方已导入的远端区；接收方在收到完成通知后校验                                                                                                                            | 提交返回非零 batch，任务状态为 COMPLETED；目标区域逐字节等于源区域；目标区域前后各 64 B 保护区域不变；源区域不变                                                                                      |
| L1.3 | 在同一连接与注册区上重复提交；连续查询两次状态；释放 batch；再提交一次                                                                                                                                        | 查询返回 OK 且 `statuses[0]` 为 COMPLETED，两次查询结果一致；`free_batch` 成功；释放后再次提交成功且数据正确。count 与提交数量不符或 batch 未知时返回 INVALID，且调用者数组不被改写                   |
| L1.4 | 一条零长度 WRITE：`length = 0`、`local_address = NULL`、`remote_address = 0`                                                                                                                                  | 作为一条任务被接受，进入 COMPLETED，不需要注册或导入内存；目标缓冲区内容不变                                                                                                                          |
| L1.5 | 逐项构造非法提交，除空数组与空指针外，每条非法请求之前都放一条合法 WRITE：count=0、requests=NULL、未注册的本地范围、未导入的远端范围、远端地址超出导入范围、地址加长度溢出、未连接 peer 的 WRITE、未知 opcode | 返回对应的 INVALID / INVALID_STATE；`*batch` 为 0，不产生 batch；batch 中所有请求都没有被执行（目标缓冲区保持原值）；随后提交合法批次仍然成功。验收同时检查目标缓冲区，确认失败的提交没有产生任何写入 |
| L1.6 | 创建后立即销毁；连续执行创建/销毁循环                                                                                                                                                                         | `destroy_engine` 返回 OK；创建/销毁循环不失败                                                                                                                                                         |

验收命令：

```sh
source "$HOME/.config/rdma101/env"
cd "$HOME/rdma101-labs"
uv run --locked python scripts/test.py {c|cpp|rust} -m lab1 -v
```

## 下一实验

[实验二](02-async.md) 仍然只有一条 WRITE，但把投递与完成处理交给自行实现的后台线程，使提交返回与传输完成分离。
