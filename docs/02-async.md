# 实验二：异步提交与非阻塞查询

**实验目标**　在实验一的同步 WRITE 基础上，把投递与完成处理移入 Engine 的后台 worker，使 `submit_transfer` 可以在传输尚未结束时就返回，应用可以随时查询任务状态并在传输期间继续工作。

**教材**　[2.4 Queue Pair](https://renfeng.org/RDMA101/02-programming-model/04-qp/)、[2.5 Completion Queue](https://renfeng.org/RDMA101/02-programming-model/05-cq/)、[2.7 RDMA WRITE](https://renfeng.org/RDMA101/02-programming-model/07-rdma-write/)

**前置**　完成[实验一](01-write.md)：Engine 已能同步完成一次 4 KiB WRITE，资源的创建、导出与回收都已实现。

## 背景

实验一结束时，一次 WRITE 已经被整理成可复用的引擎：设备和注册区只建立一次，连接也一直有效。但 `submit_transfer` 仍然是一个同步函数——它投递 WR，然后留在函数里守着 CQ，直到这条 WR 完成才返回。整个传输期间，应用线程都停在这个函数里。

### 等待是同步实现引入的

完成通知本来就是异步的。WR 投递出去之后，设备在后台搬运数据，完成时把 CQE 放进 CQ；RDMA 这样设计，正是为了让 CPU 在提交之后去做别的事。实验一把"等 CQ"写进 `submit_transfer`，只是为了简化实验内容。这段等待并不属于接口本身，只要把它从提交路径挪到后台，应用就有了和传输重叠的能力：在搬运当前数据块的同时，可以在另一块缓冲区里准备下一份。

### 传输由后台 worker 推进

要把等待挪到后台，自然需要一个后台 worker 取推进传输：

- **worker** 投递 WR、轮询 CQ，按 `wr_id` 找到对应任务，把完成结果写进任务状态；
- **应用线程** 只提交请求、查询状态，不参与投递和轮询；
- **设备** 负责实际的搬运，worker 只是接收它的完成通知。

提交与完成从此是两个时刻：`submit_transfer` 返回，表示请求已经被接受、进入了 Engine 的队列，但不表示传输已经开始或完成；`get_transfer_statuses` 读到 COMPLETED，才表示这条任务的本地设备访问已经结束、源缓冲区可以复用。两者之间的状态就是 PENDING。

### 接口语义随之变化

实验二的接口与实验一一致，但是语义相应发生了改变：

| 接口                         | 实验二的变化                             |
| ---------------------------- | ---------------------------------------- |
| `r101_create_engine`         | 启动后台 worker                          |
| `r101_submit_transfer`       | 校验并接受请求后返回，不等待传输结束     |
| `r101_get_transfer_statuses` | 只读取当前状态；尚未结束时返回 PENDING   |
| `r101_free_batch`            | 仍有 PENDING 任务时返回 BUSY             |
| `r101_destroy_engine`        | 先停止后台 worker 与设备访问，再回收资源 |

## 实验内容

### 范围与边界

**实验边界：** 实验二只要求把一次 WRITE 的投递与完成处理从应用线程移到后台 worker：`submit_transfer` 只接受请求并返回 batch，传输由 worker 推进。具体实现可以缩小为只处理一个 peer、一个存活 batch 和其中的一条连续 RDMA WRITE，同一时刻只有一条 WR 在途。

本实验不要求在途多片：worker 可以等这一条 WR 完成后，再处理下一次提交。[实验三](03-slices.md) 把一次提交扩展为多条任务与切片，[实验四](04-window.md) 再引入多条 WR 同时在途与多个 batch。

**不在本实验范围内：** 多请求与切片（实验三）；多个 batch 与发送窗口（实验四）；批量投递与选择性信号（实验五）。SEND、RECV、WRITE_IMM、READ、Atomic 在实验六至八实现，收到时返回 UNIMPLEMENTED 即可。

## 实现要点

**1. 启动与停止 worker。** `r101_create_engine` / `Engine::create` 在设备资源创建成功之后启动 worker；`r101_destroy_engine` / 析构 / `Drop` 先通知 worker 退出并等待它结束，再释放设备资源。初始化失败时不得留下正在运行的线程。

**2. submit 只接受请求。** `r101_submit_transfer` / `Engine::submit_transfer` 完成校验、复制描述、建立任务记录并入队，返回非零 batch，不得在函数内等待完成。任务记录保存投递所需的最小信息（本地范围、远端范围、opcode），不引用调用者的请求数组。

**3. CQE 驱动状态推进。** worker 投递 WR 后轮询 CQ，按 `wr_id` 定位任务。成功 CQE 把任务推进到 COMPLETED；错误 CQE 把任务记为 FAILED 并保存具体错误。

**4. 查询只读。** `r101_get_transfer_statuses` 只读取任务状态并填写调用者的数组，不投递、不轮询、不等待完成、不触发调度。

**5. free_batch 的边界。** batch 内只要还有 PENDING 任务，`free_batch` 就返回 BUSY，且 batch 保持有效、可以继续查询；全部任务进入终态后才可以释放记录。

**6. 销毁顺序。** 先停止 worker 对任务与应用缓冲区的访问，再释放 QP、MR、CQ、PD 与 context。销毁可以发生在任务仍 PENDING 时：`destroy_engine` 不等待传输完成，但必须保证 Engine 不再触碰这些缓冲区。

**7. 缓冲区的借用期。** 任务进入 COMPLETED 或 FAILED 之前，WRITE 的源缓冲区不得修改；应用只有在查询到终态之后才能复用该缓冲区。与实验一不同，这个区间现在会跨越应用线程的多次调用，因此不能再用"`submit_transfer` 返回时已经完成"来推断。

## 验收

实验二的测例会按下面的要求检查你的 Engine 行为：

| 编号 | 场景与输入                                                                                             | 预期行为                                                                                                                                       |
| ---- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| L2.1 | 提交一条明显长于一次查询的 WRITE；提交后立即连续查询；接收方在完成后校验                               | 提交返回非零 batch，且不要求返回时已经完成；传输完成前可以查询到 PENDING；继续查询直到 COMPLETED；目标区逐字节等于源、前后各 64 B 保护区不变。 |
| L2.2 | 任务处于 PENDING 时反复调用 `get_transfer_statuses`；等待终态后再查询两次                              | 每次查询都立即返回，不阻塞到传输完成；状态序列单调，PENDING 之后只出现 COMPLETED 或 FAILED，不回退；终态稳定，两次查询结果一致                 |
| L2.3 | 对一条仍 PENDING 的任务调用 `free_batch`；等任务进入终态后再调用；释放后在同一连接与注册区上再提交一次 | PENDING 时返回 BUSY，且 batch 仍然有效、可以继续查询；终态后返回 OK；释放后再次提交成功且数据正确，连接与注册区仍然可用                        |
| L2.4 | 连续执行多轮"提交 → 查询到终态 → 释放"，每轮校验数据与保护区                                           | 每轮返回新的非零 batch，标识不复用；每轮任务都能完成、数据正确                                                                                 |
| L2.5 | 在任务仍 PENDING 时调用 `destroy_engine`                                                               | 返回 OK，停止 worker 并回收资源，不等待传输完成、不挂起、不崩溃；随后重新创建引擎，能够再完成一次 WRITE                                        |

验收命令：

```sh
source "$HOME/.config/rdma101/env"
cd "$HOME/rdma101-labs"
uv run --locked python scripts/test.py {c|cpp|rust} -m lab2 -v
```

## 下一实验

[实验三](03-slices.md) 扩展任务描述：一次提交包含多条搬运任务，每条任务按内部切片大小切分为多个 WR。后台线程仍可逐片等待，先保证范围映射与完成条件正确。
