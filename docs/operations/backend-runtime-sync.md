# Backend 同步升级与无副作用检查

本页负责 API、执行 Worker 和维护 Worker 的同批升级；完整迁移与恢复仍以[部署指南](deployment.md)为准。解释身份的含义见[输入、调整与执行身份](../design/skill-interpreter-stability.md)。

## 正常调用保持不变

API 和两个 Worker 使用 `skills/service_wiring.py` 组装 SkillService。API 与执行 Worker 注入原 `build_skill_interpreter` 返回的同一组配置，资源目录、已安装 Provider 和存储设置在共享入口生成；维护 Worker 不加载解释器。组装不查询业务库、不验证外部连接、不调用模型。

本次接线不改变系统 Skill、Candidate、生成提示或 Interpreter pipeline 版本，不重新计算历史 Manifest 或 Run checksum。普通导入、无参数 Skill 的空对象输入、无显式修改范围的追加调整、已发布任务及调度保持现有入口，不增加必填参数、登录步骤、重新解释要求或每请求的部署检查。资源授权、发布门禁、效果批准和未知结果处理仍由原实现负责。

解释器未配置时，确定性导入和已发布任务的解析/加载不因同步诊断而被拒绝；实际模型解释及任务执行仍需要各自原有的有效配置。这不是保证任意 Skill 或外部服务都可执行。

## 同步更新

按现有构建、image export 和传送流程准备同一批 `images.tar`、`compose.yml`、`.env`、`Makefile`，在目标部署目录运行：

```sh
make deploy
```

该流程保留原基础设施初始化、migration-plan、迁移与 readiness 顺序，然后在同一个 Compose up 中强制重建 `api worker maintenance`。等待 API 和 Worker 心跳检查后，核对实际容器 image ID 与配置 fingerprint，最后恢复 Web。执行 Worker 的健康检查只读 ARQ 心跳，不创建测试 Run 或调用模型。

这是协调重启，不是零停机升级。升级前按部署指南处理活动执行与备份；停止容器不证明上游模型或外部写入停止，不因更新自动重跑未知请求。没有新增数据库迁移，不删除 volume，不改 Codex 登录目录，也不修改业务资源或现有批准。

## 独立检查

```sh
make runtime-check
```

该命令检查所有已运行的 API、执行 Worker、维护 Worker 容器，包括额外 replica。实际 image ID 必须等于本地本批后端 image；随后在每个容器里只读组装配置，比较解释身份、catalog、profile、queue 和功能开关。模型、业务数据库、Outbox、Run、发布版本均不被执行或写入。配置输出不含 credential 或连接原文。

默认比较失败只让本次命令返回非零，不停止或修复现有容器、不改冻结请求。`make deploy` 内的比较失败会使部署报告失败并不继续恢复 Web；已启动的后端仍可能运行，应先排查，不能把检查失败当作全局停写证明。解释器双方都未配置会明确警告，但不把一致性检查变成导入或旧任务的新门禁。

查看某个容器的详细公开配置：

```sh
docker compose --env-file .env exec -T api python -m skillmind.ops.runtime_identity
docker compose --env-file .env exec -T worker python -m skillmind.ops.runtime_identity
```

一致的 fingerprint 只证明当前 image/配置一致，不证明已登录、外部网络可达、持久请求成功或业务结果正确。诊断是新的短命本地进程读取同一容器的配置，不是已运行模型进程的状态探针。

## 历史请求与回归

本次接线自身不改变 PR #1 后的模型身份。已发布 Skill 和已创建 Run 继续使用原冻结版本，不为了部署检查强制重导入。更早版本的排队解释请求若绑定不同提示或实际模型配置，仍按原完整性和授权流程处理；UNKNOWN 不自动换 ID、补造 return 或重新请求模型。

局部回归分为共享装配测试、实际 Makefile 加 Docker fake 的非破坏测试，以及 `tests/skills/test_backend_handoff.py` 的真实 parser/adapter/compiler 配合 fake completion 的交接测试。后者不使用真实业务 DB 或外部模型，不替代实际数据库事务、SDK 登录、部署与业务验收。按[本地开发](../development/local-development.md)选择隔离测试，不直接运行会创建/删除真实数据库的全套测试。
