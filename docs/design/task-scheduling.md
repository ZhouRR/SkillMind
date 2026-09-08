# TaskSchedule 设计

> 现行实现。调度只决定何时、以谁的身份、使用哪份配置调用普通 Run 创建服务。

## 生命周期与发火

```text
创建/修改规则 → 计算下次时间 → ACTIVE
                                  ↓ Worker tick
                      CAS 认领并推进下次 occurrence
                                  ↓
                        检查创建者、版本、资源与重叠
                                  ↓
                       RunService.create_task_run
                                  ↓
                         记录本次发火结果或失败
```

支持 `ONCE` 和 `CRON`，必填 IANA timezone。定义、预览和发火复用同一时间求值逻辑；保存前 CRON 展示至少三次预览，ONCE 展示一次。状态为 `ACTIVE / PAUSED / COMPLETED / ERROR / ARCHIVED`，实际转换见 [domain](../../PJM/backend/src/projectmind/schedules/domain.py)。

## 保存和执行边界

| 规则 | 行为 |
| --- | --- |
| 版本 | 保存精确 SkillVersion 与 task_key，不自动跟随 latest |
| 输入和资源选择 | 保存 input/sources 选择规则；发火时经普通 Run 创建校验并生成本次资源快照，不把保存 Schedule 当成冻结全部外部内容 |
| 创建者 | 发火前重新检查用户有效性和 Project access |
| 幂等 | 使用 schedule ID 与 occurrence 生成创建 Run 的稳定键 |
| 重叠 | 发火前发现同 Task 非终态 Run 时跳过，包含等待输入/批准 |
| 停机错过 | 记录错过次数，不批量补跑 |
| 配置失效 | 记录 FAILED_PRECONDITION 并进入 ERROR，修正后显式恢复 |
| 结束条件 | ONCE 消化、end_at 或 max_runs 达到后停止 |

调度不会绕过审批、预授权、资源再验证、Event/Outbox 或 Result 不可变规则。UI 的暂停操作控制 Schedule 后续触发，不能当作暂停正在运行的 Agent。

document 的显式“全集”应在每个新 Run 创建时固定成员；单份/集合继续指向原 ID，不跟随同路径重新上传的内容。同一 occurrence 重放必须复用首次创建的 Run/快照。这条跨层链路仍在 R01 联调中，见[资源快照](resource-snapshots.md#创建重放与调度)。

## 可靠性保证的限度

CAS 认领与创建 Run 分属不同事务。当前代码先推进 occurrence 再创建 Run；两者之间 Worker 崩溃可能漏掉一次触发。幂等键可抑制同一 occurrence 重复创建，不能把此设计提升为“精确一次投递”。

重叠检查是创建前查询，不是任务级互斥锁。多个 Schedule 或手动创建并发发生时，不能由这次查询推导出“同 Task 全局绝不并行”。当前策略是尽量跳过已观察到的重叠；若后续需要强保证，应先设计数据库互斥/约束与统一创建事务。

如未来要求可靠补发，需引入持久 occurrence/outbox 与明确的 missed/catch-up 策略，再定义崩溃恢复；不能悄悄补跑旧时间点改变当前产品语义。

## 实现与验收

[service](../../PJM/backend/src/projectmind/schedules/service.py) · [repository](../../PJM/backend/src/projectmind/schedules/repository.py) · [cron](../../PJM/backend/src/projectmind/schedules/cron.py) · [API](../../PJM/backend/src/projectmind/api/routes/schedules.py) · [Web](../../PJM/web/src/components/ScheduleDialog.tsx)

- 本地：timezone/DST、预览、状态冲突、CAS、配置失效、既有重叠和幂等键。
- 部署：观测实际 occurrence、重叠跳过、停机错过与 ERROR 后显式恢复。
- 不承诺：条件监控、任务排队、跨 Project 调度、全局精确一次或全局同 Task 串行。

Worker tick 健康只证明调度循环在运行，不证明业务 Run 已成功创建。
