# 实验六：消息、通知与 Inline

**实验目标**　在同一个 Engine 中支持双边通信与带通知的写入：SEND、RECV、WRITE_IMM，以及 WRITE、SEND、WRITE_IMM 的 Inline 投递。

**教材**　[2.6 SEND/RECV 操作](https://renfeng.org/RDMA101/02-programming-model/06-send-recv/)、[2.6.4 RNR（Receiver Not Ready）错误](https://renfeng.org/RDMA101/02-programming-model/06-send-recv/)、[2.7.3 RDMA WRITE with Immediate](https://renfeng.org/RDMA101/02-programming-model/07-rdma-write/)、[2.4.7 标志位（send_flags）](https://renfeng.org/RDMA101/02-programming-model/04-qp/)、[2.5.10 高级主题：共享 CQ](https://renfeng.org/RDMA101/02-programming-model/05-cq/)

**前置**　完成[实验四](04-window.md)：Engine 已有后台 worker、切片与在途窗口。

## 背景

到实验五为止，Engine 做的都是单边操作：发起方投递 WR，对端只是被动地被读或被写，从不提交请求。本实验补上另外两类操作——双边通信（SEND / RECV）和带通知的写入（WRITE_IMM），以及短消息的 Inline 投递。

它们在 verbs 里和单边操作是同一类对象：都往 SQ 或 RQ 投递 WR，完成都落在 CQ 上。所以 Engine 不需要第二套接口，复用现有的接受、切片、窗口和回收逻辑即可。SEND/RECV 的 verbs 用法可以参考教材示例 [`examples/programming_model/03_rc_loopback.c`](https://github.com/alogfans/RDMA101/blob/main/examples/programming_model/03_rc_loopback.c)。

真正新增的，是"接收"这件事带来的语义。

### 接收方也要投递

单边操作里，接收方只需要准备好一块已注册的内存。SEND 不同：它必须匹配对端预先投递的一条 RECV，否则发送方会进入 RNR 重试，耗尽后以 `IBV_WC_RNR_RETRY_EXC_ERR` 结束。所以做接收的一方要提前把接收容量放进队列，并像维护发送窗口一样维护接收队列的水位。

而且一条 RECV 只接收一次：完成后不会自动重新投递。应用取走内容、准备再次使用时，要提交新的 RECV（教材 2.6.3 的 WR 生命周期）。

### 一条 SEND 就是一条消息

SEND 和单边操作不同：一条 SEND WR 只能匹配对端的一条 RECV WR，整条消息要一次放进那份接收容量。这是 verbs 的模型——发送方没有"把一条逻辑消息拆到多条 RECV"的手段。硬拆成多个 SEND 的话，每一片都会变成对端的一条独立消息，而 `receive` 里只有一个长度、没有分片信息，接收方拼不回去。

所以 SEND 不切片，应用要保证消息放得进接收区：数据量更大时，应用可能会用单边 WRITE 把数据送过去、再补一条短 SEND 通知，或者自己做分帧与重组。

### 完成时要带回"收到了什么"

消息类任务的完成不只是"成功了"，还要告诉应用收到了什么，所以状态里多了 `receive` 字段：

| `receive.kind`   | 含义             | 数据位置                                                                               |
| ---------------- | ---------------- | -------------------------------------------------------------------------------------- |
| `RECV_MESSAGE`   | 收到一条消息     | 位于 RECV 提供的缓冲区，`receive.length` 为实际长度                                    |
| `RECV_WRITE_IMM` | 收到一次到达通知 | 位于 WRITE_IMM 指定的远端地址；RECV 缓冲区不被修改，`receive.immediate` 为通知携带的值 |

`RECV_WRITE_IMM` 是其中特殊的一种：RECV 在这里不搬运数据，只用来报告"远端那次带通知的写入到了"。它可以不带缓冲区（`local_address = NULL`、`length = 0`，对应 `num_sge = 0`）。零容量 RECV 也能接收空 SEND，但装不下非空消息。

### 通知与 immediate

WRITE_IMM 在普通 WRITE 的基础上带一个 32 位值，远端对应的一条 RECV 完成时就能读到它。它同样消耗一条 RECV，需要像 SEND 一样维护接收水位。这个值在公共接口上是主机序的无符号整数；教材 2.6.5 与 2.7.3 里的 `htonl` / `ntohl` 是 verbs 字段的要求，由 Engine 在边界处做一次，应用不用再转。

### 短消息可以内联

消息很短时，把源缓冲区注册成 MR、再让网卡去读，代价可能比消息本身还大。Inline 投递（`IBV_SEND_INLINE`）让 WR 直接把数据带在请求里，源缓冲区不需要注册。它能不能用，取决于 QP 实际协商出的 `max_inline_data`，所以要用创建 QP 后查到的真实值作阈值，而不是一个固定常数。

## 实验内容

### 范围与边界

**实验边界：** 实验六把请求类型扩展到 SEND、RECV、WRITE_IMM，并支持 WRITE、SEND、WRITE_IMM 的 Inline 投递。它们复用现有的接受、窗口与回收逻辑，WRITE_IMM 还复用切片逻辑；不引入第二套接口。

本实验还要求 Engine 支持与多个 peer 同时通信，并能独立关闭其中一个 peer。关闭时，结束该 peer 的未完成任务并回收其连接资源，其余 peer 的传输继续推进。

每个 peer 在实现上可以使用一个 QP，发送和接收分别使用它的 SQ 与 RQ，RECV 不计入发送窗口。所有 peer 可以共用一个后台 worker 和一个 CQ，通过 `wr_id` 与 CQE 的 opcode 区分任务及完成类型。

**不在本实验范围内：** SEND 的切片与大消息重组（应用保证消息放得进接收区）；故障恢复与连接重建；错误 CQE 之后的重试；同一 peer 使用多 QP、多 CQ、多后台 worker。

## 实现要点

**1. 消息往返。** 应用提交形如以下的请求数组：

```python
from rdma101 import Recv, Request, Send

entries = [
    Request(peer=peer, local_address=recv_address, length=256, operation=Recv()),
    Request(peer=peer, local_address=send_address, length=message_length, operation=Send()),
]
```

实现要求：

- RECV 映射为 `ibv_post_recv`，用成功 CQE 的 `byte_len` 与 opcode 更新对应任务；
- RQ 与 SQ 各自独立：RECV 不计入 `window`。共享 CQ 时通过 `wr_id` 与 CQE 的 opcode 区分两类完成；
- 批内任务之间没有顺序依赖。RECV 位于数组首位并不意味着 SEND 需要等待它完成，否则双方会互相等待。

create_peer 成功后，QP 已在 INIT，即可提交 RECV，不必等到 connect_peer。后台负责及时预投递；submit 返回不保证 WR 已经进入 RQ。SEND 等其他操作仍须等待双方连接就绪。零长度 SEND 仍是一条消息，必须与远端 RECV 匹配，不能作为本地空操作完成。

**2. 接收不足。** 对端缺少可用 RECV 时，设备进入 RNR 重试路径，重试耗尽后发送方得到 `IBV_WC_RNR_RETRY_EXC_ERR`。本实验的处理方式是由应用预先提交足够的接收任务、后台及时投递，因此你不需要在 Engine 内考虑这个问题。

**3. 带通知的写入。** 应用把一段输入写入远端指定位置，并附带一个 32 位值。实现要求：

- 支持 WRITE_IMM 的切片，且一条逻辑任务只产生一次通知，通知到达时全部数据已可读；
- 一种可行实现是全部切片走同一条 RC QP，前面的切片使用 `IBV_WR_RDMA_WRITE`，最后一片使用 `IBV_WR_RDMA_WRITE_WITH_IMM`；
- 通知消耗远端一条 RECV，因此接收端需要像对待 SEND 一样维护接收队列水位；
- length 为 0 时仍投递一次 `IBV_WR_RDMA_WRITE_WITH_IMM`，用于纯通知；没有数据范围，不查找本地或远端 MR，WR 的 `num_sge`、`remote_addr` 与 `rkey` 可置为 0；

**4. Inline。** WRITE、SEND、WRITE_IMM 请求带 `INLINE` 标志时（对应 `IBV_SEND_INLINE`）：

- 阈值取创建 QP 后查得的实际 `max_inline_data`，不使用固定常数；所有数据 WR 都必须满足 Inline 条件。WRITE、WRITE_IMM 可以切片，SEND 保持一条消息；无法满足时，submit 在接受前返回 `UNSUPPORTED`，整批不接受，不回退；
- 本地源缓冲区可以不注册，须保持有效、可读；非空远端写入目标和 SEND 的非空接收区仍须注册。
- 借用期不变：`submit_transfer` 成功不代表后台线程已完成投递，源缓冲区的生命周期仍受任务终态约束；
- immediate 与 Inline 是不同机制：immediate 是送达对端的 32 位元数据，Inline 是本地的投递方式。

**5. 关闭连接。** 应用关闭 peer 时，接收队列中可能仍有未匹配的 RECV。`destroy_peer` 应终止该 peer 的工作并完成本地清理，不能等待对端补发消息，也不能因为还有 PENDING 任务而返回 BUSY。

先停止该 peer 的新工作，再终止已投递的 WR；可以通过将 QP 切换到 `IBV_QPS_ERR` 处理在途工作和未匹配接收。销毁开始时仍为 PENDING 的任务，在设备和 worker 不再访问其缓冲区后，记为 `CANCELED`；已经记录的终态不变。

## 验收

实验六的测例会按下面的要求检查你的 Engine 行为：

| 编号 | 场景与输入                                                                                 | 预期行为                                                                                                                                                                                                                                                                        |
| ---- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| L6.1 | 同一批提交 RECV 与 SEND，消息非空，接收区带保护区                                          | 两条任务都 COMPLETED；RECV 返回 `RECV_MESSAGE` 且 `receive.length` 等于实际消息长度；接收区内容正确、保护区不变                                                                                                                                                                 |
| L6.2 | 多轮复用同一接收区，每轮重新提交 RECV                                                      | 每轮都能收到并校验正确                                                                                                                                                                                                                                                          |
| L6.3 | 提交一条不会被匹配的 RECV 后调用 `free_batch`；再提交匹配的 SEND 使其完成                  | 未匹配时 `free_batch` 返回 BUSY 且 batch 保留；匹配完成后任务进入终态，`free_batch` 成功                                                                                                                                                                                        |
| L6.4 | 存在未匹配 RECV 时调用 `destroy_peer`，然后查询并释放 batch                                | `destroy_peer` 返回 OK，不等待对端、不因 PENDING 而返回 BUSY；该 peer 的 PENDING 任务变为 `CANCELED`，已终态不变；其他 peer 的任务继续推进                                                                                                                                      |
| L6.5 | 建连前提交 RECV（含零容量）与 WRITE、SEND；建连后提交空 SEND 与纯 IMM 通知（`length = 0`） | 建连前只有 RECV 被接受，零容量 RECV 不需要注册内存；WRITE 与 SEND 返回 INVALID_STATE。建连后空 SEND 与纯 IMM 都能与对端 RECV 匹配并完成：空 SEND 得到 `RECV_MESSAGE` 且 `receive.length` 为 0，纯 IMM 得到 `RECV_WRITE_IMM` 且 `receive.immediate` 正确；本地源缓冲区不需要注册 |
| L6.6 | 一次很长的 WRITE_IMM 携带一个 immediate 值，重复多轮                                       | 一条逻辑任务只产生一次通知；通知到达时全部数据可读且保护区正确；`receive.kind` 为 `RECV_WRITE_IMM`、`receive.immediate` 与发送值一致（主机序）；接收端 RECV 缓冲区未被覆盖                                                                                                      |
| L6.7 | 从未注册的源发送 1、127、128、129、256 字节消息，并声明 Inline；记录各长度的结果           | 能放进实际 `max_inline_data` 的请求成功且内容正确；放不下的在接受前返回 UNSUPPORTED，整批不接受、不消费 RECV、不回退到普通投递                                                                                                                                                  |
| L6.8 | 同一批混合单边 WRITE 与 SEND/RECV                                                          | 两类完成通过 `wr_id` 与 opcode 正确区分；每条任务的状态与结果各自正确；RECV 不计入发送窗口                                                                                                                                                                                      |

验收命令：

```sh
source "$HOME/.config/rdma101/env"
cd "$HOME/rdma101-labs"
uv run --locked python scripts/test.py {c|cpp|rust} -m lab6 -v
```

## 下一实验

[实验七](07-read.md) 扩展数据方向：READ，由需要数据的一方主动拉取。
