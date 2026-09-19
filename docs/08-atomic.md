# 实验八：原子更新

**实验目标**　支持 COMPARE_SWAP 与 FETCH_ADD：对远端 8 字节对齐的 64 位值执行原子读改写，并把操作前的值写回本地结果区。

**教材**　[2.9 Atomic 操作](https://renfeng.org/RDMA101/02-programming-model/09-atomic/)，其中 [2.9.2 Compare & Swap（CAS）](https://renfeng.org/RDMA101/02-programming-model/09-atomic/)、[2.9.3 Fetch & Add（FA）](https://renfeng.org/RDMA101/02-programming-model/09-atomic/)、[2.9.4 Atomic 操作的限制](https://renfeng.org/RDMA101/02-programming-model/09-atomic/)

**前置**　完成[实验七](07-read.md)：Engine 已经会管理远端的响应方资源。Atomic 与 READ 共用同一份 `max_rd_atomic` 预算。

## 背景

WRITE 和 READ 各自只做一件事——写，或者读——都没法把"读出来、判断、再写回去"当成一个整体。两个进程同时做的时候，读和写有可能会交错：以一把用 0 表示空闲、1 表示占用的远端锁为例，A 读到 0、准备写回 1，而 B 在 A 写回之前也读到了 0——于是两个人都以为自己拿到了锁。

原子操作把"读—改—写"交给远端网卡一次完成，中间不会被别的请求打断。标准 verbs 的远端原子操作只有 COMPARE_SWAP 和 FETCH_ADD，本实验把它们都实现。

### 两个 opcode

| opcode         | verbs                         | 语义                                                        |
| -------------- | ----------------------------- | ----------------------------------------------------------- |
| `COMPARE_SWAP` | `IBV_WR_ATOMIC_CMP_AND_SWP`   | 远端值等于 `args.atomic.compare` 时写入 `args.atomic.value` |
| `FETCH_ADD`    | `IBV_WR_ATOMIC_FETCH_AND_ADD` | 远端值加上 `args.atomic.value`                              |

### 两个操作都返回操作前的值

设备不把"这次操作的结果"（交换有没有发生或加完是多少）放进返回值，它只把**操作前的值**写回本地结果区。

- **COMPARE_SWAP**：返回值是交换前的值。应用拿它和 `compare` 比一下才知道交换有没有发生——相等说明换了，不等说明远端值已经被别人改过。注意"有没有换成"和"操作成不成功"是两回事：不管换没换成，这次操作都正常完成，CQE 都是成功。
- **FETCH_ADD**：返回值就是加法之前的值。拿它领号，天然不会有两个人拿到同一个号；如果应用需要加法后的新值，本地加上去即可。

这个旧值直接由设备写回请求指定的本地缓冲区。所以每条 Atomic 请求都必须提供一段 8 字节的已注册内存作为结果区。

### 和 READ 共用同一份响应方资源

原子请求也需要远端网卡为它保留状态，所以它和 READ 用的是同一份响应方资源：`max_rd_atomic` 与 `max_dest_rd_atomic` 的约束在实验七已经建立，这里依然适用。

## 实验内容

### 范围与边界

**实验边界：** 实验八只要求支持 COMPARE_SWAP 与 FETCH_ADD，作用在远端 8 字节对齐的 64 位值上，并把操作前的值写回本地结果区。一次请求对应设备上的一次原子读改写；是否重试、如何解释返回值由应用决定。实现上每个 peer 可以只使用一个 QP，所有 peer 共用一个后台 worker 和一个 CQ。

**不在本实验范围内：** 用多条 QP 分摊 Atomic 负载；QP 出错后的重连与恢复。

## 实现要点

**1. verbs 映射。** 两个 opcode 分别对应上表中的 verbs 操作；`remote_address` 指向远端 8 字节值，操作前的值写入 `local_address`。

**2. 接受阶段的校验。** 校验在接受前完成，失败返回 INVALID：

- 本地与远端地址均按 8 字节对齐；
- `length` 必须等于 8；
- 远端 MR 具有 `REMOTE_ATOMIC`，并按 `ibv_reg_mr` 的要求同时具有 `LOCAL_WRITE`。

**3. 结果可用。** 与实验七相同：结果位于本地内存，任务进入 COMPLETED 后才可读取。

**4. 并发资源。** Atomic 与 READ 占用同一份远端响应方资源，因此实验七设置的并发上限在此同样适用；一次提交的 Atomic 数量远多于该上限时，应由 Engine 自己排队，而不是让设备返回 `IBV_WC_REM_OP_ERR`。

**5. 能力不足时的返回。** 设备或 provider 不支持该 opcode 时返回 `UNSUPPORTED`。

## 验收

实验八的测例会按下面的要求检查你的 Engine 行为：

| 编号 | 场景与输入                                                                                                                                                    | 预期行为                                                                                                              |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| L8.1 | 对初值为 v 的远端 64 位值提交两条 CAS：一条 `compare=v`，一条 `compare=v+1`                                                                                   | 两条都 COMPLETED 并返回操作前的值；命中的那条把远端值改为 `value`；未命中的那条不改变远端值                           |
| L8.2 | 两个发起者从第三个进程的计数器上各执行 1000 次 `FETCH_ADD(value=1)`，然后竞争一个初值为 0 的 CAS 状态                                                         | 两个发起者取得的旧值集合合起来恰好是 0..1999，不重复、不缺失；远端计数器最终为 2000；CAS 竞争只有一个发起者取得旧值 0 |
| L8.3 | 逐项构造非法 Atomic：本地或远端地址未按 8 字节对齐、`length` 不等于 8、远端 MR 缺少 `REMOTE_ATOMIC`；另尝试注册仅带 `REMOTE_ATOMIC`、不带 `LOCAL_WRITE` 的 MR | 非法提交返回 INVALID，整批不接受、不产生 batch；非法注册也返回 INVALID；随后提交合法 Atomic 仍然成功                  |
| L8.4 | 一次提交的 Atomic 数量远多于 `max_rd_atomic`，并与 READ 混在同一批                                                                                            | 全部成功，没有请求因 `REM_OP_ERR` 失败                                                                                |

验收命令：

```sh
source "$HOME/.config/rdma101/env"
cd "$HOME/rdma101-labs"
uv run --locked python scripts/test.py {c|cpp|rust} -m lab8 -v
```

## 下一实验

完成本实验后，Engine 已支持单边搬运、双边通信、到达通知、按需拉取与原子更新，可以用于构建应用。
