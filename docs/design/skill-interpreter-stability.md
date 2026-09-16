# Skill 解释的输入、调整与执行身份

本页补充 [Skill 解释与发布实现](skill-interpretation.md)，只约束新候选的准备声明，不生成第二份业务执行计划，不转换或回填旧 SkillVersion / Run。

## 调用输入根类型

新 Candidate v2 的 `input_contract` 根 `type` 固定为 `object`，与 `CreateTaskRunRequest.input` 一致。生成 Schema 与 `direct_candidate.compile_candidate` 分别检查。对象内的字段和数组元素仍可使用原来的标量、数组和对象类型；不改变通用 TaskContractDraft v1、历史 Manifest 或旧结果契约。无用户输入使用空对象契约。

## 追加调整与修复

原文执行版追加调整的冻结请求在父摘要之外携带 `previous_interpretation.launch_contract` 及 checksum。它是从已保存 Manifest 精确投影的完整准备声明，包含标题、说明、输入及来源、资源及来源、Tool 和诊断，不是对历史模型原始输出的恢复，也不包含第二份原文或生成后的 Schema。旧 Blueprint 保持摘要兼容，不逆转换为新声明。

`SkillService.accept_interpretation_request` / `adjust_interpretation` 可接收可选的 `editable_paths`，路径以该准备声明为根，最多 32 条。显式范围与父声明一同冻结并计入执行键，候选完整编译后再比较；增加、删除、数组顺序及值类型变化均不能越过范围，越界失败，不静默回滚部分候选。例：只允许修改第一个输入字段的必填性时使用 `/input_contract/fields/0/required`。范围不是自然语言推断结果，也不是权限授予。

本次没有新增 HTTP 参数或 Web 范围选择控件；现有无范围的追加调整继续接收完整父声明，但不声称程序保证“只修改自然语言指定的部分”。接入 HTTP / Web 时须同步 DTO、OpenAPI、客户端校验及明确选择交互。旧 Blueprint 不支持显式新声明范围。

自动校验修复仍最多一次。有可唯一定位的 Candidate v2 component 错误时，允许修改该 component，其他 component 必须保持原值；缺失与 null 不等同。位置不明确或汇总多个原因的发布前校验失败，仍走原有的一次完整候选修复和全部门禁，不假称具备局部保护。修复失败沿用 `schema_validation_failed`，不发布部分修复结果。

## 实际模型配置冻结

生产 wiring 为两种引擎构造 `InterpreterRuntimeProfile`：引擎、实际模型、实际思考强度、固定 SDK/CLI 版本、操作目录 checksum、文本 JSON fallback 和输入投影/编译流水线版本。Claude 的路由及输出限制使用固定非凭据字段集合的摘要区分；API key / auth token 不进入该摘要，endpoint 和目录不作为明文配置发布。

`ModelSkillInterpreter` 在构造时复制操作目录；实际模型输入使用同一副本。生产 `prompt_checksum` 包含原系统 Skill / Schema identity、实际组成的系统提示和上述 profile。受理、Worker 重建校验和结果审计使用同一份 defensive snapshot。因此 effort、SDK/CLI、操作目录或提示变化不能被当作原请求重放。流水线投影或编译语义变化须更新 `INTERPRETER_PIPELINE_VERSION`。

两种解释 transport 均未实现 per-call `temperature`、`seed` 等参数；非空参数现在明确拒绝，不再静默丢弃。参数拒绝发生在模型调用前，不返回提供的 key/value，不自动切换模型或参数。底层 `compute_execution_key` 的通用算法与旧无 profile 的离线 fixture identity 保持兼容；生产 wiring 必须始终提供 profile。

## 部署和验证边界

API 与 Worker 同步升级。生产身份变化会使旧排队请求与新部署重建输入不一致；保留原请求及 UNKNOWN 等已有状态，按既有输入完整性错误处理，不能用新身份自动重启未知模型调用。已发布 Manifest 和旧 Run 不重写、不重新计算 checksum。

回归覆盖 object root / nested 类型、空输入、父声明副本、范围越界、单 component 修复、实际配置影响执行键及无效参数拒绝。纯 helper/Schema 测试与实际 service/adapter 的 fake-completion 接线测试分开；两者均不证明真实模型语义稳定率、真实 DB 事务或外部写入效果。字段名兼容、默认值、条件输入、业务执行门禁和大型 Skill 按需加载不在本次变更范围。
