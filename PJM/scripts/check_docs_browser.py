"""自己完結した文書を実 browser で検証する。アプリ・DB・外部 API は操作しない。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Page, Route, async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
TASK_TARGETS = (
    ("R01 资源", "r01-资源冻结"),
    ("R02 预算", "r02-run-统一预算"),
    ("R03 Flow", "r03-task-flow-完整链路"),
    ("R04 生成模块", "r04-生成模块"),
    ("R05 身份", "r05-领域与身份安全"),
    ("R06 Skill", "r06-skill-生命周期"),
    ("R07 Run", "r07-run-与审计"),
    ("R08 效果", "r08-外部效果"),
    ("R09 调度", "r09-调度"),
    ("R10 Web", "r10-全部-web-页面"),
    ("R11 工程与运维", "r11-运维与工程工具"),
    ("R12 业务质量", "r12-业务质量验收"),
    ("R13 全量审计", "r13-全量契约与最终审计"),
)
SECTION_TARGETS = (
    ("docs/design/document-lifecycle.md", "一个例子列表消失不等于清理完成"),
    ("docs/design/document-lifecycle.md", "身份目录与公开面"),
    ("docs/design/document-lifecycle.md", "上传的三个边界"),
    ("docs/design/document-lifecycle.md", "保存可靠性的修正要求"),
    ("docs/design/document-lifecycle.md", "读取下载与预览"),
    ("docs/design/document-lifecycle.md", "删除与历史引用"),
    ("docs/design/document-lifecycle.md", "引用保护与清理的修正要求"),
    ("docs/design/document-lifecycle.md", "页面与结果未知"),
    ("docs/design/document-lifecycle.md", "开发接续与验收"),
    ("docs/operations/runbook.md", "文档保存与删除的只读分诊"),
    ("PJM/backend/README.md", "project-文書の保存と清理を追う"),
    ("PJM/contracts/README.md", "project-文書の保存と読取を読む"),
    ("PJM/web/README.md", "project-文書の管理を追う"),
    ("docs/design/project-lifecycle.md", "一个例子归档不是停止或删除"),
    ("docs/design/project-lifecycle.md", "项目身份与成员资格"),
    ("docs/design/project-lifecycle.md", "成员管理的现状与目标"),
    ("docs/design/project-lifecycle.md", "项目选择与失效链接"),
    ("docs/design/project-lifecycle.md", "归档的实际边界"),
    ("docs/design/project-lifecycle.md", "并发修改不能只看有无行锁"),
    ("docs/design/project-lifecycle.md", "删除与数据保留"),
    ("docs/design/project-lifecycle.md", "删除门禁的修正要求"),
    ("docs/design/project-lifecycle.md", "开发接续与验收"),
    ("docs/operations/runbook.md", "项目与归档的只读分诊"),
    ("PJM/backend/README.md", "project-とメンバーの管理を追う"),
    ("PJM/contracts/README.md", "project-と-membership-の契約を読む"),
    ("PJM/web/README.md", "project-の切替と管理を追う"),
    ("docs/design/results-evaluation.md", "先分清四种事实"),
    ("docs/design/results-evaluation.md", "一个例子执行结束结论仍需修订"),
    ("docs/design/results-evaluation.md", "结果的形状与读取来源"),
    ("docs/design/results-evaluation.md", "结果校验的实际保证"),
    ("docs/design/results-evaluation.md", "引用可信性的修正要求"),
    ("docs/design/results-evaluation.md", "保存与显示不是同一个提交"),
    ("docs/design/results-evaluation.md", "评价请求与历史"),
    ("docs/design/results-evaluation.md", "修订指向哪份原值"),
    ("docs/design/results-evaluation.md", "提交未知与界面责任"),
    ("docs/design/results-evaluation.md", "兼容与开发接续"),
    ("docs/design/results-evaluation.md", "验收条件"),
    ("docs/operations/runbook.md", "结果与评价的只读分诊"),
    ("PJM/backend/README.md", "結果検証と人工評価を追う"),
    ("PJM/contracts/README.md", "結果と人工修訂の契約を読む"),
    ("PJM/web/README.md", "結果と人工評価を接続する"),
    ("docs/design/user-interactions.md", "先分清三种人工参与"),
    ("docs/design/user-interactions.md", "一个例子回答超时不等于什么都没发生"),
    ("docs/design/user-interactions.md", "提问与答复的实际形状"),
    ("docs/design/user-interactions.md", "普通提问不能代替外部批准"),
    ("docs/design/user-interactions.md", "三个提交边界"),
    ("docs/design/user-interactions.md", "首次答复与原答复重放"),
    ("docs/design/user-interactions.md", "过期与拒绝响应"),
    ("docs/design/user-interactions.md", "答复界面与结果未知"),
    ("docs/design/user-interactions.md", "验收条件"),
    ("docs/design/agent-runtime.md", "8-用户交互协议"),
    ("docs/design/workspace.md", "普通答复与续行状态"),
    ("PJM/backend/README.md", "通常回答と期限処理を追う"),
    ("PJM/contracts/README.md", "通常回答と評価の契約を読む"),
    ("docs/design/resource-snapshots.md", "用一个例子理解冻结边界"),
    ("docs/design/resource-snapshots.md", "读取清单和资源摘要"),
    ("docs/design/task-scheduling.md", "时间输入与展示的边界"),
    ("docs/development/contract-workflow.md", "历史数据兼容不等于前后端版本兼容"),
    ("docs/development/contract-workflow.md", "遇到未接齐的交付链"),
    ("PJM/contracts/README.md", "run-文書契約を読む"),
    ("docs/planning/roadmap.md", "13-当前执行状态"),
    ("docs/operations/runbook.md", "环境文件与配置边界"),
    ("docs/operations/runbook.md", "一致恢复点包含什么"),
    ("docs/operations/runbook.md", "恢复前的停止条件"),
    ("docs/operations/runbook.md", "恢复后验证"),
    ("docs/operations/runbook.md", "旧部署与恢复入口"),
    ("docs/operations/deployment.md", "先分清四种操作"),
    ("docs/operations/deployment.md", "一个例子关闭-dispatch-后仍有工作"),
    ("docs/operations/deployment.md", "环境文件与配置边界"),
    ("docs/operations/deployment.md", "迁移前置与执行"),
    ("docs/operations/deployment.md", "迁移与回退审查"),
    ("docs/operations/deployment.md", "会话协议切换检查"),
    ("docs/operations/deployment.md", "启动与放行"),
    ("docs/operations/deployment.md", "后续开发约束与验收"),
    ("docs/operations/backup-recovery.md", "先选路径"),
    ("docs/operations/backup-recovery.md", "一个例子恢复不能抹掉后来的事实"),
    ("docs/operations/backup-recovery.md", "一致恢复点包含什么"),
    ("docs/operations/backup-recovery.md", "取得并检查数据库备份"),
    ("docs/operations/backup-recovery.md", "恢复前的停止条件"),
    ("docs/operations/backup-recovery.md", "替换数据库"),
    ("docs/operations/backup-recovery.md", "恢复后验证"),
    ("docs/operations/backup-recovery.md", "应用版本回退"),
    ("PJM/backend/README.md", "運用-cli-と停止境界を確認する"),
    ("PJM/images/README.md", "作成から転送まで"),
    ("PJM/images/README.md", "配備と復旧の境界"),
    ("docs/design/resource-snapshots.md", "产物与访问"),
    ("docs/design/resource-snapshots.md", "一次准备的提交边界"),
    ("docs/design/resource-snapshots.md", "准备中断与再次使用"),
    ("docs/design/resource-snapshots.md", "跨根总量的修正口径待实现"),
    ("docs/design/agent-runtime.md", "74-从领取到模型启动的边界"),
    ("docs/design/agent-runtime.md", "75-取消超时与失去执行权"),
    ("docs/design/run-budgets.md", "现有计时器的覆盖范围"),
    ("docs/operations/runbook.md", "准备故障的只读分诊"),
    ("PJM/backend/README.md", "入力準備の接続を引き継ぐ"),
    ("docs/README.md", "目的から探す"),
    ("docs/design/task-flow.md", "计划身份与显示布局"),
    ("docs/design/task-flow.md", "从发布到历史重放"),
    ("docs/design/task-flow.md", "一个例子计划不等于执行事实"),
    ("docs/design/skill-contract.md", "发布与就绪的判断顺序"),
    ("docs/design/skill-contract.md", "111-版本内容与可见性"),
    ("docs/design/skill-contract.md", "112-可审计的重新启用与回滚"),
    ("docs/design/skill-interpretation.md", "从候选到项目任务的接线"),
    ("docs/design/repository-effects.md", "先分清四种事实"),
    ("docs/design/repository-effects.md", "调用与批准链路"),
    ("docs/design/repository-effects.md", "阶段回执与不确定结果"),
    ("docs/design/repository-effects.md", "执行权与取消"),
    ("docs/design/workspace.md", "审批请求与执行结果"),
    ("PJM/backend/README.md", "承認から外部変更まで追う"),
    ("PJM/contracts/README.md", "外部変更の契約を読む"),
    ("docs/design/task-scheduling.md", "一个例子规则触发与执行分别看"),
    ("docs/design/task-scheduling.md", "生命周期与发火"),
    ("docs/design/task-scheduling.md", "重叠检查到底看谁"),
    ("docs/design/task-scheduling.md", "保存后的管理入口"),
    ("docs/design/task-scheduling.md", "认领记录与恢复权限"),
    ("docs/design/task-scheduling.md", "配置并发与暂停"),
    ("docs/design/task-scheduling.md", "结算计数与未知结果"),
    ("docs/design/task-scheduling.md", "历史兼容与实施顺序"),
    ("PJM/backend/README.md", "schedule-の認領と回写を追う"),
    ("PJM/contracts/README.md", "schedule-の公開契約を読む"),
    ("PJM/web/README.md", "調度の保存と管理を引き継ぐ"),
    ("docs/design/run-budgets.md", "用量现在流向哪里"),
    ("docs/design/run-budgets.md", "计量报告如何归一化"),
    ("docs/design/run-budgets.md", "一个例子已用占用与可用"),
    ("docs/design/run-budgets.md", "持久账本的当前载体"),
    ("docs/design/run-budgets.md", "内部状态不能当作运行证明"),
    ("docs/design/run-budgets.md", "启动与结算的提交边界"),
    ("docs/design/run-budgets.md", "执行权与结算权分开"),
    ("docs/design/run-budgets.md", "上线门禁与接线顺序"),
    ("docs/design/subagents.md", "当前返回值的可信边界"),
    ("docs/design/subagents.md", "完成失败与审计如何表示"),
    ("PJM/backend/README.md", "予算と子分析の接続を追う"),
    ("PJM/backend/README.md", "台帳の実装を引き継ぐ"),
    ("docs/operations/runbook.md", "迁移与回退审查"),
    ("PJM/contracts/README.md", "子分析と用量の契約を読む"),
    ("PJM/web/README.md", "子分析と用量を読む"),
    ("docs/design/subagents.md", "一个例子完成的是哪一层"),
    ("docs/design/subagents.md", "子任务指令与结果的边界"),
    ("docs/design/subagents.md", "提交顺序与版本兼容"),
    ("docs/design/subagents.md", "开发接续顺序"),
    ("docs/design/run-supervision.md", "一个例子点击取消之后"),
    ("docs/design/run-supervision.md", "不同阶段如何收束"),
    ("docs/design/run-supervision.md", "原因与执行权如何分类"),
    ("docs/design/run-supervision.md", "收尾终态与晚到信息"),
    ("docs/design/run-supervision.md", "兼容与开发接续"),
    ("docs/design/run-supervision.md", "验收矩阵"),
    ("PJM/backend/README.md", "実行の取消と停止を追う"),
    ("docs/design/run-supervision.md", "提交时谁决定最终状态"),
    ("docs/design/run-supervision.md", "等待提交仍是独立边界"),
    ("docs/planning/roadmap.md", "当前证据怎么用"),
    ("docs/history/README.md", "実行とデータ"),
    ("docs/history/README.md", "文書と運用"),
    ("docs/development/documentation.md", "状态与证据"),
    ("docs/development/documentation.md", "目录语言与阅读"),
    ("docs/development/documentation.md", "设计评审的核对点"),
    ("docs/development/documentation.md", "按改动选择验证范围"),
    ("docs/development/documentation.md", "自动检查各自证明什么"),
    ("PJM/web/README.md", "取消と最終状態を表示する"),
    ("PJM/contracts/README.md", "run-の取消と終態を読む"),
    ("docs/history/delivery-history.md", "723-文档验证与保留范围"),
    ("docs/design/authentication.md", "先分清四类凭据"),
    ("docs/design/authentication.md", "一个例子同一账号打开两个页面"),
    ("docs/design/authentication.md", "会话失效与权限变化"),
    ("docs/design/authentication.md", "会话读取与多页面"),
    ("docs/design/authentication.md", "会话凭据-v2-与切换要求"),
    ("docs/design/authentication.md", "缓存与代理"),
    ("docs/design/authentication.md", "从请求到项目内操作"),
    ("docs/design/authentication.md", "认证与业务提交不是同一个事务"),
    ("docs/design/authentication.md", "72-平台托管managed应用层信封加密"),
    ("docs/design/authentication.md", "验收从场景出发"),
    ("docs/design/authentication.md", "用户生命周期与管理事务"),
    ("docs/design/authentication.md", "用户操作与公开面"),
    ("docs/design/authentication.md", "事务与并发"),
    ("docs/design/authentication.md", "生效与界面"),
    ("docs/design/user-lifecycle.md", "一个例子停用再启用不恢复旧登录"),
    ("docs/design/user-lifecycle.md", "工作副本与公开入口"),
    ("docs/design/user-lifecycle.md", "场景与非目标"),
    ("docs/design/user-lifecycle.md", "用户操作与目标公开面"),
    ("docs/design/user-lifecycle.md", "版本查询与公开数据"),
    ("docs/design/user-lifecycle.md", "改密入口的配额"),
    ("docs/design/user-lifecycle.md", "事务与并发"),
    ("docs/design/user-lifecycle.md", "审计与请求关联"),
    ("docs/design/user-lifecycle.md", "生效与界面"),
    ("docs/design/user-lifecycle.md", "迁移与历史兼容"),
    ("docs/design/user-lifecycle.md", "开发接续与验收"),
    ("docs/design/domain-model.md", "账户与会话"),
    ("PJM/backend/README.md", "ユーザー管理の接続を引き継ぐ"),
    ("PJM/contracts/README.md", "ユーザー管理の公開面を準備する"),
    ("PJM/web/README.md", "アカウント管理を接続する"),
    ("docs/design/login-protection.md", "一个例子一次登录两次入口请求"),
    ("docs/design/login-protection.md", "保护范围与执行顺序"),
    ("docs/design/login-protection.md", "计数与退避如何恢复"),
    ("docs/design/login-protection.md", "短期状态与失败关闭"),
    ("docs/design/login-protection.md", "公开响应与客户端责任"),
    ("docs/design/login-protection.md", "提交离页与结果未知"),
    ("docs/design/login-protection.md", "来源识别与上线边界"),
    ("docs/design/login-protection.md", "开发接续与验收"),
    ("PJM/backend/README.md", "ログイン入口の防護を追う"),
    ("docs/operations/runbook.md", "登录防护的排查与恢复"),
    ("docs/development/local-development.md", "隔离-redis-登录防护验证"),
    ("docs/development/local-development.md", "実-postgresql-の前提"),
    ("docs/development/local-development.md", "ログイン-client-の局部回帰"),
    ("docs/development/local-development.md", "ログイン画面のブラウザ回帰"),
    ("docs/design/secret-storage.md", "一个例子保存成功不等于资源可用"),
    ("docs/design/secret-storage.md", "三种来源的边界"),
    ("docs/design/secret-storage.md", "managed-的实际加密结构"),
    ("docs/design/secret-storage.md", "明文与威胁边界"),
    ("docs/design/secret-storage.md", "切换与恢复的顺序"),
    ("docs/design/secret-storage.md", "开发接续与验收"),
    ("docs/operations/runbook.md", "认证故障的只读分诊"),
    ("docs/operations/runbook.md", "会话协议切换检查"),
    ("docs/operations/runbook.md", "88-managed-secret-の-kek-運用"),
    ("PJM/backend/README.md", "認証と-secret-の境界を追う"),
    ("PJM/web/README.md", "ログインと書込失敗を切り分ける"),
    ("PJM/contracts/README.md", "認証と-secret-の契約を読む"),
    ("docs/history/delivery-history.md", "733-文档验证与保留范围"),
    ("docs/design/generated-modules.md", "先分清三种模块与预览"),
    ("docs/design/generated-modules.md", "一个例子图表坏了任务没有失败"),
    ("docs/design/generated-modules.md", "目标与流水线"),
    ("docs/design/generated-modules.md", "现有代码与公开契约"),
    ("docs/design/generated-modules.md", "决策与安全边界"),
    ("docs/design/generated-modules.md", "origin-与-csp"),
    ("docs/design/generated-modules.md", "当前-csp-常量不是投放配置"),
    ("docs/design/generated-modules.md", "静态拒绝与依赖"),
    ("docs/design/generated-modules.md", "构建网络"),
    ("docs/design/generated-modules.md", "构建输入与结果的提交"),
    ("docs/design/generated-modules.md", "host-协议"),
    ("docs/design/generated-modules.md", "挂载切换与晚到消息"),
    ("docs/design/generated-modules.md", "版本与回退"),
    ("docs/design/generated-modules.md", "内容身份与历史兼容"),
    ("docs/design/generated-modules.md", "展示选择与全局停用"),
    ("docs/design/generated-modules.md", "实施顺序与验收"),
    ("docs/design/generated-modules.md", "从场景验收"),
    ("docs/design/workspace.md", "10-frontendmodule-生命周期"),
    ("docs/design/workspace.md", "13-生成模块回退"),
    ("PJM/backend/README.md", "生成-module-の前置実装を読む"),
    ("PJM/web/README.md", "生成表示と業務-module-を分ける"),
    ("PJM/contracts/README.md", "生成-module-と既存-module-api-を分ける"),
    ("docs/history/delivery-history.md", "743-文档验证与保留范围"),
    ("docs/history/delivery-history.md", "753-文档验证与保留范围"),
    ("docs/history/delivery-history.md", "763-文档验证与保留范围"),
    ("docs/acceptance/jaf-quality.md", "按目的阅读"),
    ("docs/acceptance/jaf-quality.md", "4-迁移改造规则"),
    ("docs/acceptance/jaf-quality.md", "10-benchmark-v1"),
    ("docs/acceptance/jaf-quality.md", "143-smoke-与-benchmark"),
    ("docs/acceptance/jaf-benchmark.md", "先看一次评价的边界"),
    ("docs/acceptance/jaf-benchmark.md", "哪些数据交给谁"),
    ("docs/acceptance/jaf-benchmark.md", "样本数量"),
    ("docs/acceptance/jaf-benchmark.md", "选取与冻结"),
    ("docs/acceptance/jaf-benchmark.md", "case-定义"),
    ("docs/acceptance/jaf-benchmark.md", "配对与重跑"),
    ("docs/acceptance/jaf-benchmark.md", "文本-rubric"),
    ("docs/acceptance/jaf-benchmark.md", "指标口径"),
    ("docs/acceptance/jaf-benchmark.md", "一个例子失败也留在分母中"),
    ("docs/acceptance/jaf-benchmark.md", "硬性技术门禁"),
    ("docs/acceptance/jaf-benchmark.md", "首版质量目标"),
    ("docs/acceptance/jaf-benchmark.md", "记录对象与当前载体"),
    ("docs/acceptance/jaf-benchmark.md", "平台-evaluation-与发布结论"),
    ("docs/acceptance/jaf-benchmark.md", "报告内容"),
    ("docs/development/change-guide.md", "运行与数据"),
    ("docs/development/change-guide.md", "skill-与界面"),
    ("docs/development/change-guide.md", "身份与交付"),
    ("docs/development/change-guide.md", "特别需要先修正的差距"),
    ("docs/history/delivery-history.md", "783-文档验证与保留范围"),
    ("docs/history/delivery-history.md", "793-保留与交付"),
    *(("docs/planning/roadmap.md", anchor) for _label, anchor in TASK_TARGETS),
)


def page_url(book: Path, document: str, anchor: str = "") -> str:
    """build と同じ文書 ID/章 anchor を file URL の hash に渡す。"""

    return f"{book.as_uri()}#{quote(document, safe='/')}::{quote(anchor, safe='')}"


async def settle(page: Page) -> None:
    """navigation 後の scroll/table 測定が完了するまで描画 frame を待つ。"""

    await page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


async def check_layout(page: Page, label: object) -> None:
    """横長の表だけを scroll 領域にし、文書全体の画面外へのはみ出しを検出する。"""

    await settle(page)
    result = await page.evaluate("""() => ({
      width: document.documentElement.scrollWidth, viewport: innerWidth,
      labels: [...document.querySelectorAll('.topbar .actions a, .topbar .actions button')]
        .filter(control => control.getClientRects().length).map(control => {
          const range = document.createRange();
          range.selectNodeContents(control);
          return {text: control.textContent, lines: range.getClientRects().length};
        }),
      tables: [...document.querySelectorAll('#main .table-scroll')].map(region => ({
        overflow: region.querySelector('table').getBoundingClientRect().width
          > region.clientWidth + 1,
        hint: !region.previousElementSibling.hidden, focusable: region.tabIndex === 0
      }))
    })""")
    assert result["width"] <= result["viewport"], (label, result)
    assert all(item["lines"] == 1 for item in result["labels"]), (label, result)
    assert all(
        item["overflow"] == item["hint"] == item["focusable"] for item in result["tables"]
    ), (label, result)


async def check_heading(page: Page) -> None:
    """focus だけでなく、章の全見出しが固定 header の下に見えていることを確認する。"""

    await settle(page)
    result = await page.evaluate("""() => {
      const heading = document.querySelector('#main h2:focus, #main h3:focus, #main h4:focus');
      if (!heading) return null;
      const bar = document.querySelector('.topbar');
      const rect = heading.getBoundingClientRect();
      return {top: rect.top, bottom: rect.bottom, viewport: innerHeight,
        header: getComputedStyle(bar).position === 'sticky'
          ? bar.getBoundingClientRect().bottom : 0};
    }""")
    assert result is not None, (page.url, "no focused section heading")
    assert result["top"] >= result["header"] + 8, (page.url, result)
    assert result["bottom"] <= result["viewport"], (page.url, result)


async def screenshot(page: Page, output: Path | None, name: str) -> None:
    """明示された保存先だけに画面を保存し、既定実行で生成物を増やさない。"""

    if output is not None:
        await page.screenshot(path=str(output / f"{name}.png"))


async def check_search_and_navigation(page: Page, book: Path, output: Path | None) -> None:
    """章検索・履歴の分離・keyboard・戻る/進むを、同じ閲覧版の実操作で確認する。"""

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(book.as_uri())
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")
    await screenshot(page, output, "guide-desktop")
    await page.locator("#main a", has_text="設計ガイド").click()
    await expect(page.locator("#main h1")).to_have_text("設計の読み順と責任分担")
    await page.locator("#main a", has_text="Run 作成・幂等").click()
    await expect(page.locator("#main h1")).to_have_text("Run 创建、重发与幂等")
    await page.go_back()
    await expect(page.locator("#main h1")).to_have_text("設計の読み順と責任分担")
    await page.go_forward()
    await expect(page.locator("#main h1")).to_have_text("Run 创建、重发与幂等")

    # 初回表示・brand・不明文書からの復帰を同じ目的別 guide に揃える。
    await page.locator(".brand").click()
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")
    await page.goto(page_url(book, "docs/not-a-document.md"))
    await expect(page.locator("#main h1")).to_have_text("文档不存在")
    await page.get_by_role("link", name="返回文档导航", exact=True).click()
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")

    await page.locator("#search").fill("准备世代")
    # 同じ本文語が複数章に現れるのは正常。件数を一件に制限せず、意図した章への移動を守る。
    target = page.locator('#navigation .search-section a[href*="resource-snapshots.md"]').filter(
        has_text="一次准备的提交边界"
    )
    await expect(target).to_have_count(1)
    assert not await page.locator('#navigation a[href*="/history/"]').count()
    await target.click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("一次准备的提交边界")
    await page.evaluate("window.scrollTo(0, 0)")
    await target.click()
    await check_heading(page)

    await page.locator("#search").fill("contract-workflow.md")
    first = page.locator("#navigation .search-hit > a").first
    assert (await first.get_attribute("href") or "").startswith(
        "#docs/development/contract-workflow.md::"
    )
    await first.click()
    await expect(page.locator("#main h1")).to_have_text("公开契约变更与联调")
    await screenshot(page, output, "contract-desktop")

    await page.locator("#search").fill("字段缺失默认成空集合")
    await page.locator("#navigation .search-section a").first.focus()
    await page.keyboard.press("Enter")
    await check_heading(page)
    await expect(page.locator("#navigation mark").first).to_have_text("字段缺失默认成空集合")
    result = await page.evaluate("""() => {
      const node = searchExcerpt({text:'<img src=x onerror=alert(1)> keyword'}, 'keyword');
      return {images:node.querySelectorAll('img').length,
        mark:node.querySelector('mark').textContent, text:node.textContent};
    }""")
    assert result["images"] == 0 and result["mark"] == "keyword" and "<img" in result["text"]
    await page.locator("#search").fill("<img src=x onerror=alert(1)>")
    assert not await page.locator("#navigation img, #navigation script").count()
    await expect(page.locator("#search-status")).to_have_text("0 篇匹配文档")
    await page.locator("#search").fill("2026")
    await page.locator("#include-history").check()
    assert await page.locator('#navigation a[href*="/history/"]').count() > 0
    await page.locator("#search").fill("")
    await page.locator("#include-history").uncheck()

    await page.goto(page_url(book, "docs/design/resource-snapshots.md", "用一个例子理解冻结边界"))
    await check_heading(page)
    await screenshot(page, output, "resources-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(page_url(book, "docs/design/resource-snapshots.md"))
    await page.locator(".inline-toc summary").focus()
    await page.keyboard.press("Enter")
    await page.locator(".inline-toc a", has_text="读取清单和资源摘要").focus()
    await page.keyboard.press("Enter")
    await check_heading(page)
    await check_layout(page, "resources mobile")
    await screenshot(page, output, "resources-mobile")
    await page.locator("#menu-button").click()
    await page.locator("#search").fill("字段缺失默认成空集合")
    await page.locator("#navigation .search-section a").first.focus()
    await page.keyboard.press("Enter")
    await check_heading(page)
    await expect(page.locator("#menu-button")).to_have_attribute("aria-expanded", "false")
    await page.locator("#menu-button").click()
    await expect(page.locator("#search")).to_be_focused()
    await page.keyboard.press("Escape")
    await expect(page.locator("#menu-button")).to_have_attribute("aria-expanded", "false")
    await expect(page.locator("#menu-button")).to_be_focused()

    await page.goto(page_url(book, "docs/design/task-scheduling.md", "时间输入与展示的边界"))
    await check_heading(page)
    await screenshot(page, output, "schedule-mobile")
    await page.emulate_media(media="print")
    assert not await page.locator(".inline-toc").is_visible()
    assert not await page.locator(".sidebar").is_visible()
    await page.emulate_media(media="screen")
    for anchor in (
        "134-最近文档核对2026-09-08",
        "137-创建与调度设计续整2026-09-08",
        "1310-公开契约交接与章节检索2026-09-08",
        "1312-运维恢复与工作副本边界核对2026-09-08",
        "26-任务流程视图",
    ):
        await page.goto(page_url(book, "docs/planning/roadmap.md", anchor))
        await check_heading(page)
    for name in ("business-structure.html", "technical-architecture.html"):
        await page.goto((book.parent / "overview" / name).as_uri())
        await check_layout(page, name)

    # 復元点の比較表と破壊操作前の条件も、運用者が読む幅で視認できる証拠を残す。
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/operations/runbook.md", "一致恢复点包含什么"))
    await check_heading(page)
    await screenshot(page, output, "operations-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(page_url(book, "docs/operations/runbook.md", "恢复前的停止条件"))
    await check_heading(page)
    await check_layout(page, "operations mobile")
    await screenshot(page, output, "operations-mobile")

    # 論理 path と実際の公開条件を混同しないよう、新しい準備 flow と中断表も抽看する。
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/design/resource-snapshots.md", "一次准备的提交边界"))
    await check_heading(page)
    await screenshot(page, output, "preparation-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(page_url(book, "docs/design/resource-snapshots.md", "准备中断与再次使用"))
    await check_heading(page)
    await check_layout(page, "preparation mobile")
    await screenshot(page, output, "preparation-mobile")

    # 新しい入口から Flow の具体例まで、移動と実際の読み幅を確認する。
    await page.goto(page_url(book, "docs/README.md", "目的から探す"))
    await check_heading(page)
    await check_layout(page, "guide mobile")
    await screenshot(page, output, "guide-mobile")
    await page.locator("#main a", has_text="設計ガイド").click()
    await page.locator("#main a", has_text="Flow の具体例").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("一个例子：计划不等于执行事实")  # noqa: RUF001
    await check_layout(page, "flow mobile")
    await screenshot(page, output, "flow-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/design/task-flow.md", "计划身份与显示布局"))
    await check_heading(page)
    await screenshot(page, output, "flow-desktop")

    # 実装案内から開始条件、計時器、取消処理へ進む実際の読書経路を守る。
    await page.goto(page_url(book, "PJM/backend/README.md", "入力準備の接続を引き継ぐ"))
    await page.locator("#main a", has_text="計時器の正本").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("现有计时器的覆盖范围")
    await screenshot(page, output, "timeouts-desktop")
    await page.locator("#main a", has_text="Runtime §7.5").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("7.5 取消、超时与失去执行权")
    await page.goto(page_url(book, "docs/design/agent-runtime.md", "74-从领取到模型启动的边界"))
    await check_heading(page)
    await screenshot(page, output, "runtime-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await check_layout(page, "runtime mobile")
    # 同一 hash への goto は再移動しないため、窄幅で deep link を実際に読み直す。
    await page.reload()
    await check_heading(page)
    await screenshot(page, output, "runtime-mobile")
    await page.goto(page_url(book, "docs/operations/runbook.md", "准备故障的只读分诊"))
    await check_heading(page)
    await check_layout(page, "preparation triage mobile")
    await screenshot(page, output, "triage-mobile")

    # 実行資産の README から、公開状態と回退の設計へ迷わず進めることを守る。
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "PJM/skills/README.md"))
    await page.locator("#main a", has_text="公開と就緒の判断順").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("发布与就绪的判断顺序")
    await screenshot(page, output, "skill-stages-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "skill stages mobile")
    await screenshot(page, output, "skill-stages-mobile")
    await page.locator("#main a", has_text="升级回滚").click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("11. 版本、回滚与评价")
    await page.goto(page_url(book, "docs/design/skill-contract.md", "111-版本内容与可见性"))
    await check_heading(page)
    await check_layout(page, "skill rollback mobile")
    await screenshot(page, output, "skill-rollback-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/design/skill-interpretation.md", "从候选到项目任务的接线"))
    await check_heading(page)
    await screenshot(page, output, "skill-lifecycle-desktop")

    # 批准と外部結果を混同しない読書経路を、実装案内から狭幅の復旧設計まで辿る。
    await page.goto(page_url(book, "PJM/backend/README.md", "承認から外部変更まで追う"))
    await page.locator("#main a", has_text="批准と外部結果の違い").click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("先分清四种事实")
    await screenshot(page, output, "effects-facts-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "effects facts mobile")
    assert await page.locator("#main .table-scroll").first.evaluate(
        "region => region.scrollWidth <= region.clientWidth"
    ), "The first effect-state table must be readable without horizontal scrolling"
    await screenshot(page, output, "effects-facts-mobile")
    await page.goto(page_url(book, "docs/design/repository-effects.md", "调用与批准链路"))
    await check_heading(page)
    await check_layout(page, "effects transactions mobile")
    await screenshot(page, output, "effects-transactions-mobile")
    await page.locator("#main a", has_text="阶段回执要求").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("阶段回执与不确定结果")
    await check_layout(page, "effects recovery mobile")
    await screenshot(page, output, "effects-recovery-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.locator("#main a", has_text="审批界面").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("审批请求与执行结果")
    await screenshot(page, output, "effects-decision-desktop")

    # Schedule の摘要を Run 履歴と誤読しないよう、実装入口から例・重複・認領を辿る。
    await page.goto(page_url(book, "PJM/backend/README.md", "schedule-の認領と回写を追う"))
    await page.locator("#main a", has_text="規則・発火・Run の具体例").click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text(
        "一个例子：规则、触发与执行分别看"  # noqa: RUF001
    )
    await screenshot(page, output, "schedule-facts-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "schedule facts mobile")
    await screenshot(page, output, "schedule-facts-mobile")
    summary = page.locator("#main .table-scroll").filter(
        has=page.get_by_role("columnheader", name="它实际说明什么", exact=True)
    )
    await expect(summary).to_have_count(1)
    assert await summary.evaluate("region => region.scrollWidth <= region.clientWidth"), (
        "Schedule summary meanings must be readable without horizontal scrolling"
    )
    # 例の下にある表も全行を抽看できる位置へ送り、固定 header の裏へ隠さない。
    await summary.evaluate("""region => window.scrollBy(0,
      region.getBoundingClientRect().top
      - document.querySelector('.topbar').getBoundingClientRect().bottom - 16)""")
    await check_layout(page, "schedule summary mobile")
    await screenshot(page, output, "schedule-summary-mobile")
    await page.locator("#main a", has_text="重叠范围").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("重叠检查到底看谁")
    await screenshot(page, output, "schedule-overlap-mobile")
    await page.locator("#main a", has_text="认领记录与执行权").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("认领记录与恢复权限")
    await check_layout(page, "schedule recovery mobile")
    await screenshot(page, output, "schedule-recovery-mobile")

    # Web の page 結合と公開 paging の責任を、文書だけのクリックで確認する。
    await page.goto(page_url(book, "PJM/web/README.md", "調度の保存と管理を引き継ぐ"))
    await page.locator("#main a", has_text="保存後の入口").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("保存后的管理入口")
    await check_layout(page, "schedule management mobile")
    await screenshot(page, output, "schedule-management-mobile")
    await page.locator("#main a", has_text="契约入口").click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("Schedule の公開契約を読む")
    await check_layout(page, "schedule contract mobile")
    await screenshot(page, output, "schedule-contract-mobile")


async def check_budget_handoff(page: Page, book: Path, output: Path | None) -> None:
    """予算の例と子結果の差距を、実装案内から実際の link で辿る。"""

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "PJM/backend/README.md", "予算と子分析の接続を追う"))
    await page.get_by_role("link", name="予算の数値例", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text(
        "一个例子：已用、占用与可用"  # noqa: RUF001
    )
    await screenshot(page, output, "budget-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "budget example mobile")
    account = page.locator("#main .table-scroll").filter(
        has=page.get_by_role("columnheader", name="时点", exact=True)
    )
    await expect(account).to_have_count(1)
    assert await account.evaluate("region => region.scrollWidth <= region.clientWidth"), (
        "The budget example must be readable without horizontal scrolling"
    )
    await screenshot(page, output, "budget-mobile")
    await page.get_by_role("link", name="执行权与结算权", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("执行权与结算权分开")
    await check_layout(page, "late usage mobile")
    await screenshot(page, output, "budget-recovery-mobile")

    await page.goto(page_url(book, "PJM/web/README.md", "子分析と用量を読む"))
    await page.get_by_role("link", name="用量の読み分け", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("用量现在流向哪里")
    await screenshot(page, output, "budget-sources-mobile")
    await page.get_by_role("link", name="子分析设计", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("当前返回值的可信边界")
    await check_layout(page, "subagent facts mobile")
    facts = page.locator("#main .table-scroll").filter(
        has=page.get_by_role("columnheader", name="观察什么", exact=True)
    )
    await expect(facts).to_have_count(1)
    assert await facts.evaluate("region => region.scrollWidth <= region.clientWidth"), (
        "Subagent facts must be readable without horizontal scrolling"
    )
    await screenshot(page, output, "subagent-gaps-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.reload()
    await check_heading(page)
    await screenshot(page, output, "subagent-gaps-desktop")
    await page.get_by_role("link", name="Tool response 与消费者", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("子分析と用量の契約を読む")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "budget contract mobile")
    await screenshot(page, output, "budget-contract-mobile")


async def check_budget_ledger_handoff(page: Page, book: Path, output: Path | None) -> None:
    """内部台帳から未接続境界・計画・移行制約まで、業務操作なしで読み継げるか確認する。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        await page.goto(page_url(book, "PJM/backend/README.md", "台帳の実装を引き継ぐ"))
        await page.get_by_role("link", name="台帳の現在地", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_text("持久账本的当前载体")
        await check_layout(page, ("budget ledger carriers", width))
        carriers = page.locator("#main .table-scroll").filter(
            has=page.get_by_role("columnheader", name="内部对象", exact=True)
        )
        await expect(carriers).to_have_count(1)
        assert await carriers.evaluate("region => region.scrollWidth <= region.clientWidth"), (
            "Ledger carriers and their limitations must be visible together"
        )
        await screenshot(page, output, f"ledger-carriers-{width}")
        await page.get_by_role("link", name="状态与运行证明", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_text("内部状态不能当作运行证明")
        await check_layout(page, ("budget ledger states", width))
        states = page.locator("#main .table-scroll").filter(
            has=page.get_by_role("columnheader", name="预留状态", exact=True)
        )
        await expect(states).to_have_count(1)
        assert await states.evaluate("region => region.scrollWidth <= region.clientWidth"), (
            "Ledger states must not hide their evidence limitations off screen"
        )
        await screenshot(page, output, f"ledger-states-{width}")
        await page.get_by_role("link", name="计划 R02", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h4:focus")).to_have_text("R02 Run 统一预算")
        await page.get_by_role("link", name="账本接续入口", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_text("台帳の実装を引き継ぐ")
        await check_layout(page, ("budget implementation handoff", width))
        await screenshot(page, output, f"ledger-implementation-{width}")
        await page.get_by_role("link", name="公開前の門禁", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_text("上线门禁与接线顺序")
        await page.get_by_role("link", name="Runbook 迁移审查", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_have_text("迁移与回退审查")
        await check_layout(page, ("budget migration review", width))
        await screenshot(page, output, f"ledger-migration-{width}")


async def check_login_protection_handoff(page: Page, book: Path, output: Path | None) -> None:
    """入口配額、会話と故障を読み分ける経路を検証し、実ログインは行わない。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        await page.goto(page_url(book, "PJM/backend/README.md", "ログイン入口の防護を追う"))
        await (
            page.locator("#main")
            .get_by_role("link", name="一回のログインと二回の入口 request", exact=True)
            .click()
        )
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_have_id("一个例子一次登录两次入口请求")
        for title in ("请求", "维度", "返回", "页面收到什么"):
            table = page.locator("#main .table-scroll").filter(
                has=page.get_by_role("columnheader", name=title, exact=True)
            )
            await expect(table).to_have_count(1)
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "Login quota, responses and their limitations must be readable together",
                width,
                title,
            )
        await check_layout(page, ("login quota example", width))
        await screenshot(page, output, f"login-quota-{width}")

        if width > 1250:
            contents = page.locator("#toc-links")
        else:
            contents = page.locator("#main .inline-toc")
            await contents.locator("summary").click()
        await contents.get_by_role("link", name="提交、离页与结果未知", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_have_id("提交离页与结果未知")
        flow = page.locator("#main pre").filter(has_text="登记在途")
        assert await flow.evaluate("region => region.scrollWidth <= region.clientWidth"), (
            "The login request lifecycle must not hide rejection or cleanup branches",
            width,
        )
        await check_layout(page, ("login request lifecycle", width))
        await screenshot(page, output, f"login-request-lifecycle-{width}")

        for name, anchor in (
            ("登录防护分诊", "登录防护的排查与恢复"),
            ("设计正本", "计数与退避如何恢复"),
            ("Web 入口", "ログインと書込失敗を切り分ける"),
            ("認証の契約入口", "認証と-secret-の契約を読む"),
            ("計画の現在証拠", "当前证据怎么用"),
        ):
            await page.locator("#main").get_by_role("link", name=name, exact=True).click()
            await check_heading(page)
            await expect(page.locator("#main :is(h2, h3):focus")).to_have_id(anchor)
            await check_layout(page, ("login protection handoff", width, anchor))
            await screenshot(page, output, f"login-{anchor}-{width}")

        await page.goto(page_url(book, "docs/planning/roadmap.md", "13-当前执行状态"))
        overview = page.locator("#main .table-scroll").filter(
            has=page.get_by_role("columnheader", name="能力", exact=True)
        )
        assert await overview.evaluate("region => region.scrollWidth <= region.clientWidth"), (
            "Current capability and remaining scope must be readable together",
            width,
        )
        await overview.get_by_role("link", name="R05", exact=True).first.click()
        await check_heading(page)
        await expect(page.locator("#main h4:focus")).to_have_id("r05-领域与身份安全")
        await screenshot(page, output, f"login-current-scope-{width}")

    await page.goto(book.as_uri())
    await page.locator("#search").fill("login-protection.md")
    first = page.locator("#navigation .search-hit > a").first
    assert (await first.get_attribute("href") or "").startswith(
        "#docs/design/login-protection.md::"
    )
    await first.click()
    await expect(page.locator("#main h1")).to_have_text("登录入口防护与失败恢复")


async def check_auth_secret_handoff(page: Page, book: Path, output: Path | None) -> None:
    """認証と Secret の読書経路を確認し、実ログインや鍵操作は行わない。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        await page.goto(page_url(book, "PJM/backend/README.md", "認証と-secret-の境界を追う"))
        for name, anchor in (
            ("多ページでの認証の例", "一个例子同一账号打开两个页面"),
            ("会话读取与多页面", "会话读取与多页面"),
        ):
            await page.locator("#main").get_by_role("link", name=name, exact=True).click()
            await check_heading(page)
            await expect(page.locator("#main :is(h2, h3):focus")).to_have_id(anchor)
            await check_layout(page, ("authentication handoff", width, anchor))
            await screenshot(page, output, f"auth-{anchor}-{width}")
        for title in ("名称", "情况"):
            table = page.locator("#main .table-scroll").filter(
                has=page.get_by_role("columnheader", name=title, exact=True)
            )
            await expect(table).to_have_count(1)
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "Credential meanings and session limitations must be readable together"
            )
        await page.goto(page_url(book, "docs/design/authentication.md", "先分清四类凭据"))
        await check_heading(page)
        await screenshot(page, output, f"auth-credentials-{width}")
        await page.locator("#main").get_by_role("link", name="会话协议与兼容", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_id("会话凭据-v2-与切换要求")
        await check_layout(page, ("session protocol", width))
        protocol = page.locator("#main .table-scroll").filter(
            has=page.get_by_role("columnheader", name="项目", exact=True)
        )
        await expect(protocol).to_have_count(1)
        assert await protocol.evaluate("region => region.scrollWidth <= region.clientWidth"), (
            "Session protocol parameters and their boundaries must be readable together"
        )
        await screenshot(page, output, f"auth-protocol-{width}")
        await page.locator("#main").get_by_role("link", name="会话协议切换检查", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_have_id("会话协议切换检查")
        await check_layout(page, ("session cutover", width))
        await screenshot(page, output, f"auth-cutover-{width}")
        await page.goto(page_url(book, "docs/design/authentication.md", "先分清四类凭据"))
        await page.locator("#main").get_by_role("link", name="认证故障分诊", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_id("认证故障的只读分诊")
        await check_layout(page, ("authentication triage", width))
        triage = page.locator("#main .table-scroll").filter(
            has=page.get_by_role("columnheader", name="原响应", exact=True)
        )
        await expect(triage).to_have_count(1)
        assert await triage.evaluate("region => region.scrollWidth <= region.clientWidth"), (
            "Authentication errors and safe next steps must be readable together",
            width,
        )
        await screenshot(page, output, f"auth-triage-{width}")

        await page.goto(page_url(book, "PJM/web/README.md", "ログインと書込失敗を切り分ける"))
        for name, anchor in (
            ("認証の契約入口", "認証と-secret-の契約を読む"),
            ("Secret の実保存形式", "managed-的实际加密结构"),
        ):
            await page.locator("#main").get_by_role("link", name=name, exact=True).click()
            await check_heading(page)
            await expect(page.locator("#main h2:focus")).to_have_id(anchor)
            await check_layout(page, ("secret contract handoff", width, anchor))
            await screenshot(page, output, f"secret-{anchor}-{width}")
        material = page.locator("#main .table-scroll").filter(
            has=page.get_by_role("columnheader", name="保存对象", exact=True)
        )
        await expect(material).to_have_count(1)
        assert await material.evaluate("region => region.scrollWidth <= region.clientWidth"), (
            "Key metadata and encryption limitations must be readable together"
        )
        await page.locator("#main").get_by_role("link", name="进程切换与恢复", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_id("切换与恢复的顺序")
        await screenshot(page, output, f"secret-rotation-{width}")
        await page.locator("#main").get_by_role("link", name="Runbook KEK 运维", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_id("88-managed-secret-の-kek-運用")
        await check_layout(page, ("secret rotation runbook", width))
        await screenshot(page, output, f"secret-runbook-{width}")


async def check_project_lifecycle_handoff(page: Page, book: Path, output: Path | None) -> None:
    """Project の選択・アーカイブ・削除を正本へ案内し、DB や業務 API は操作しない。"""

    target = "docs/design/project-lifecycle.md"
    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for document, label, anchor in (
            ("docs/design/domain-model.md", "项目生命周期", ""),
            ("docs/design/README.md", "アーカイブの具体例", "一个例子归档不是停止或删除"),
            ("docs/design/workspace.md", "项目选择", "项目选择与失效链接"),
            ("PJM/backend/README.md", "削除門禁", "删除门禁的修正要求"),
            ("PJM/contracts/README.md", "Project lifecycle", ""),
            ("PJM/web/README.md", "失効リンクの設計", "项目选择与失效链接"),
            ("docs/operations/runbook.md", "删除限制", "删除与数据保留"),
        ):
            await page.goto(page_url(book, document))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, target, anchor))
            if anchor:
                await check_heading(page)
                await page.reload()
                await check_heading(page)
            else:
                await expect(page.locator("#main")).to_be_focused()
            await check_layout(page, ("project lifecycle entry", width, document))

        for anchor, header in (
            ("项目身份与成员资格", "对象"),
            ("项目选择与失效链接", "场景"),
            ("归档的实际边界", "入口"),
            ("删除与数据保留", "现状"),
        ):
            await page.goto(page_url(book, target, anchor))
            table = page.locator("#main .table-scroll").filter(
                has=page.get_by_role("columnheader", name=header, exact=True)
            )
            await expect(table).to_have_count(1)
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "Project behavior and its limit must be readable together",
                width,
                anchor,
            )
            await check_heading(page)
            await screenshot(page, output, f"projects-{anchor}-{width}")

        for label, document, anchor in (
            ("Backend", "PJM/backend/README.md", "project-とメンバーの管理を追う"),
            ("Web", "PJM/web/README.md", "project-の切替と管理を追う"),
            ("契约", "PJM/contracts/README.md", "project-と-membership-の契約を読む"),
        ):
            await page.goto(page_url(book, target, "开发接续与验收"))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, document, anchor))
            await check_heading(page)

        await page.goto(page_url(book, target, "一个例子归档不是停止或删除"))
        flow = page.locator("#main pre").filter(has_text="Project P")
        await expect(flow).to_have_count(1)
        assert await flow.evaluate("region => region.scrollWidth <= region.clientWidth")
        await screenshot(page, output, f"projects-example-{width}")

        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("project-lifecycle.md")
        first = page.locator("#navigation .search-hit > a").first
        assert (await first.get_attribute("href") or "").startswith(f"#{target}::")
        await first.click()
        await expect(page).to_have_url(page_url(book, target))
        await expect(page.locator("#main")).to_be_focused()
        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("")


async def check_document_lifecycle_handoff(page: Page, book: Path, output: Path | None) -> None:
    """文書の保存と清理を実クリックで読み分け、upload や bucket 操作は行わない。"""

    target = "docs/design/document-lifecycle.md"
    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for document, label, anchor in (
            ("docs/design/domain-model.md", "文档生命周期", ""),
            ("docs/design/README.md", "削除と再アップロードの例", "一个例子列表消失不等于清理完成"),
            ("docs/design/resource-snapshots.md", "项目文档生命周期", ""),
            ("docs/design/workspace.md", "文档删除示例", "一个例子列表消失不等于清理完成"),
            ("PJM/backend/README.md", "文書の保存境界", "上传的三个边界"),
            ("PJM/contracts/README.md", "文書 lifecycle", ""),
            ("PJM/web/README.md", "未知結果の設計", "页面与结果未知"),
            ("docs/operations/runbook.md", "保存与清理", "删除与历史引用"),
        ):
            await page.goto(page_url(book, document))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, target, anchor))
            if anchor:
                await check_heading(page)
                await page.reload()
                await check_heading(page)
            else:
                await expect(page.locator("#main")).to_be_focused()
            await check_layout(page, ("document asset entry", width, document))

        for anchor, header in (
            ("一个例子列表消失不等于清理完成", "观察"),
            ("身份目录与公开面", "公开操作"),
            ("上传的三个边界", "当前检查"),
            ("删除与历史引用", "失败或变化"),
        ):
            await page.goto(page_url(book, target, anchor))
            table = page.locator("#main .table-scroll").filter(
                has=page.get_by_role("columnheader", name=header, exact=True)
            )
            await expect(table).to_have_count(1)
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "Document action and its guarantee must be readable together",
                width,
                anchor,
            )
            await check_heading(page)
            await screenshot(page, output, f"document-assets-{anchor}-{width}")

        for label, document, anchor in (
            ("Backend", "PJM/backend/README.md", "project-文書の保存と清理を追う"),
            ("契约", "PJM/contracts/README.md", "project-文書の保存と読取を読む"),
            ("Web", "PJM/web/README.md", "project-文書の管理を追う"),
        ):
            await page.goto(page_url(book, target, "开发接续与验收"))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, document, anchor))
            await check_heading(page)
            await check_layout(page, ("document asset code", width, document))

        await page.goto(page_url(book, target, "一个例子列表消失不等于清理完成"))
        flow = page.locator("#main pre").filter(has_text="上传 A")
        await expect(flow).to_have_count(1)
        assert await flow.evaluate("region => region.scrollWidth <= region.clientWidth")
        await screenshot(page, output, f"document-assets-example-{width}")
        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("document-lifecycle.md")
        first = page.locator("#navigation .search-hit > a").first
        assert (await first.get_attribute("href") or "").startswith(f"#{target}::")
        await first.click()
        await expect(page).to_have_url(page_url(book, target))
        await expect(page.locator("#main")).to_be_focused()
        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("")


async def check_user_lifecycle_handoff(page: Page, book: Path, output: Path | None) -> None:
    """接続済み API・未接続の消費側・失敗の読書経路を確認し、ユーザー操作はしない。"""

    target = "docs/design/user-lifecycle.md"
    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for document, label, anchor in (
            ("PJM/backend/README.md", "内部実装と公開入口", "工作副本与公开入口"),
            ("PJM/contracts/README.md", "管理 API の操作境界", "用户操作与目标公开面"),
            ("PJM/web/README.md", "アカウント画面の責務", "生效与界面"),
            ("docs/design/README.md", "停止と再有効化の例", "一个例子停用再启用不恢复旧登录"),
        ):
            await page.goto(page_url(book, document))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, target, anchor))
            await check_heading(page)
            await check_layout(page, ("user lifecycle entry", width, document))
            await screenshot(page, output, f"users-{anchor}-{width}")

        for old_anchor, anchor in (
            ("用户操作与公开面", "用户操作与目标公开面"),
            ("事务与并发", "事务与并发"),
            ("生效与界面", "生效与界面"),
        ):
            await page.goto(page_url(book, "docs/design/authentication.md", old_anchor))
            await check_heading(page)
            await page.locator("#main h3:focus + p a").first.click()
            await expect(page).to_have_url(page_url(book, target, anchor))
            await check_heading(page)
            await page.go_back()
            await check_heading(page)
            await expect(page.locator("#main h3:focus")).to_have_id(old_anchor)
            await page.go_forward()
            await page.reload()
            await check_heading(page)

        for anchor, header in (
            ("工作副本与公开入口", "层次"),
            ("生效与界面", "收到的事实"),
        ):
            await page.goto(page_url(book, target, anchor))
            table = page.locator("#main .table-scroll").filter(
                has=page.get_by_role("columnheader", name=header, exact=True)
            )
            await expect(table).to_have_count(1)
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "Account facts and their limits must be readable together",
                width,
                anchor,
            )

        for label, document, anchor in (
            ("Backend 接线入口", "PJM/backend/README.md", "ユーザー管理の接続を引き継ぐ"),
            ("公开契约入口", "PJM/contracts/README.md", "ユーザー管理の公開面を準備する"),
            ("Web 账户入口", "PJM/web/README.md", "アカウント管理を接続する"),
            ("计划 R05", "docs/planning/roadmap.md", "r05-领域与身份安全"),
        ):
            await page.goto(page_url(book, target, "开发接续与验收"))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, document, anchor))
            await check_heading(page)
            await check_layout(page, ("user lifecycle return", width, document))

        for anchor, marker in (
            ("一个例子停用再启用不恢复旧登录", "S1 / S2"),
            ("工作副本与公开入口", "HTTP 路由"),
            ("事务与并发", "入口认证"),
        ):
            await page.goto(page_url(book, target, anchor))
            flow = page.locator("#main pre").filter(has_text=marker)
            await expect(flow).to_have_count(1)
            assert await flow.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "The account transaction flow must fit without horizontal scrolling",
                width,
                anchor,
            )
            await check_heading(page)
            await screenshot(page, output, f"users-flow-{anchor}-{width}")

        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("user-lifecycle.md")
        first = page.locator("#navigation .search-hit > a").first
        assert (await first.get_attribute("href") or "").startswith(f"#{target}::")
        await first.click()
        await expect(page).to_have_url(page_url(book, target))
        await expect(page.locator("#main")).to_be_focused()
        await check_layout(page, ("user lifecycle search", width))
        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("")


async def check_contract_handoff(page: Page, book: Path, output: Path | None) -> None:
    """契約の相違を調べる入口へ進めるか確認し、掲載コマンドは実行しない。"""

    document = "docs/development/contract-workflow.md"
    anchor = "遇到未接齐的交付链"
    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for entry, label in (
            ("docs/README.md", "交付の接線確認"),
            ("docs/development/api-usage.md", "只読の確認手順"),
            ("PJM/contracts/README.md", "未接続の交付連鎖"),
            ("PJM/scripts/README.md", "保存快照の只読確認"),
        ):
            await page.goto(page_url(book, entry))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, document, anchor))
            await check_heading(page)
            await check_layout(page, ("contract handoff", width, entry))
            await page.reload()
            await check_heading(page)

        for target, section, header, filename in (
            (document, anchor, "看到的现象", "contract-drift"),
            ("PJM/backend/README.md", "一つの変更を追う", "調べたい動作", "backend-entry"),
            (
                "PJM/contracts/README.md",
                "ユーザー管理の公開面を準備する",
                "データの用途",
                "user-contracts",
            ),
        ):
            await page.goto(page_url(book, target, section))
            table = page.locator("#main .table-scroll").filter(
                has=page.get_by_role("columnheader", name=header, exact=True)
            )
            await expect(table).to_have_count(1)
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "The task and its handoff must remain readable together",
                width,
                section,
            )
            await check_heading(page)
            await check_layout(page, ("contract index", width, target))
            await screenshot(page, output, f"{filename}-{width}")


async def check_interaction_handoff(page: Page, book: Path, output: Path | None) -> None:
    """回答・批准・評価の区別を実際のリンクで辿り、通常回答の本文を窄屏でも読める。"""

    target = "docs/design/user-interactions.md"
    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for entry, label, anchor in (
            ("docs/design/agent-runtime.md", "用户答复、等待与续行", ""),
            ("PJM/backend/README.md", "普通の回答・外部批准・結果評価", "先分清三种人工参与"),
            ("PJM/web/README.md", "結果不明と現在要求の確認", "答复界面与结果未知"),
            ("PJM/contracts/README.md", "ユーザー交互の正本", ""),
            ("docs/operations/runbook.md", "410 与过期提交", "过期与拒绝响应"),
        ):
            await page.goto(page_url(book, entry))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, target, anchor))
            if anchor:
                await check_heading(page)
                await page.reload()
                await check_heading(page)
            else:
                await expect(page.locator("#main")).to_be_focused()
            await check_layout(page, ("interaction handoff", width, entry))

        for document, anchor, header in (
            (target, "先分清三种人工参与", "操作"),
            (target, "提问与答复的实际形状", "内容"),
            (target, "首次答复与原答复重放", "容易误读的值"),
            (target, "答复界面与结果未知", "用户看到的情况"),
            ("PJM/backend/README.md", "通常回答と期限処理を追う", "接続点"),
            ("PJM/contracts/README.md", "通常回答と評価の契約を読む", "用途"),
        ):
            await page.goto(page_url(book, document, anchor))
            # README の同名 header を持つ他表ではなく、対象章に続く最初の表を検査する。
            heading = page.locator(f'[id="{anchor}"]')
            table = heading.locator('xpath=following-sibling::div[@class="table-scroll"][1]')
            await expect(table.get_by_role("columnheader", name=header, exact=True)).to_have_count(
                1
            )
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "The interaction fact and its boundary must remain readable together",
                width,
                anchor,
            )
            await check_heading(page)
            await screenshot(page, output, f"interaction-{anchor}-{width}")

        await page.goto(page_url(book, target, "三个提交边界"))
        flow = page.locator("#main pre").filter(has_text="提问事务")
        await expect(flow).to_have_count(1)
        assert await flow.evaluate("region => region.scrollWidth <= region.clientWidth")
        await check_heading(page)
        await screenshot(page, output, f"interaction-flow-{width}")

        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("user-interactions.md")
        first = page.locator("#navigation .search-hit > a").first
        assert (await first.get_attribute("href") or "").startswith(f"#{target}::")
        await first.click()
        await expect(page).to_have_url(page_url(book, target))
        await expect(page.locator("#main")).to_be_focused()
        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("")


async def check_result_handoff(page: Page, book: Path, output: Path | None) -> None:
    """結果・原値・提出不明の正本へ旧章と実装案内から移動し、窄屏で比較する。"""

    target = "docs/design/results-evaluation.md"
    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for entry, label, anchor in (
            ("docs/design/agent-runtime.md", "结果、证据与人工评价", ""),
            ("docs/design/workspace.md", "原值指针", "修订指向哪份原值"),
            ("PJM/backend/README.md", "結果検証の実際の保証", "结果校验的实际保证"),
            ("PJM/contracts/README.md", "結果と評価の正本", ""),
            ("PJM/web/README.md", "評価の結果不明と切替", "提交未知与界面责任"),
            ("docs/operations/runbook.md", "评价结果未知", "提交未知与界面责任"),
        ):
            await page.goto(page_url(book, entry))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            await expect(page).to_have_url(page_url(book, target, anchor))
            if anchor:
                await check_heading(page)
                await page.reload()
                await check_heading(page)
            else:
                await expect(page.locator("#main")).to_be_focused()
            await check_layout(page, ("result handoff", width, entry))

        for document, anchor, header in (
            (target, "先分清四种事实", "看见的内容"),
            (target, "结果校验的实际保证", "检查对象"),
            (target, "修订指向哪份原值", "结果形状与指针"),
            (target, "提交未知与界面责任", "用户看到的情况"),
            ("PJM/backend/README.md", "結果検証と人工評価を追う", "接続点"),
            ("PJM/contracts/README.md", "結果と人工修訂の契約を読む", "用途"),
        ):
            await page.goto(page_url(book, document, anchor))
            heading = page.locator(f'[id="{anchor}"]')
            table = heading.locator('xpath=following-sibling::div[@class="table-scroll"][1]')
            await expect(table.get_by_role("columnheader", name=header, exact=True)).to_have_count(
                1
            )
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "The result fact and its verification limit must remain readable together",
                width,
                anchor,
            )
            await check_heading(page)
            await screenshot(page, output, f"result-{anchor}-{width}")

        await page.goto(page_url(book, target, "保存与显示不是同一个提交"))
        flow = page.locator("#main pre").filter(has_text="终态事务")
        await expect(flow).to_have_count(1)
        assert await flow.evaluate("region => region.scrollWidth <= region.clientWidth")
        await check_heading(page)
        await screenshot(page, output, f"result-commit-{width}")

        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("results-evaluation.md")
        first = page.locator("#navigation .search-hit > a").first
        assert (await first.get_attribute("href") or "").startswith(f"#{target}::")
        await first.click()
        await expect(page).to_have_url(page_url(book, target))
        await expect(page.locator("#main")).to_be_focused()
        if not await page.locator("#search").is_visible():
            await page.get_by_role("button", name="目录", exact=True).click()
        await page.locator("#search").fill("")


async def check_generated_module_handoff(page: Page, book: Path, output: Path | None) -> None:
    """生成界面の設計を辿り、module API や実 builder を動かさず可読性を確認する。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        await page.goto(page_url(book, "PJM/backend/README.md", "生成-module-の前置実装を読む"))
        for name, anchor in (
            ("表示故障の例", "一个例子图表坏了任务没有失败"),
            ("展示选择与回退", "展示选择与全局停用"),
        ):
            await page.locator("#main").get_by_role("link", name=name, exact=True).click()
            await check_heading(page)
            await expect(page.locator("#main :is(h2, h3):focus")).to_have_id(anchor)
            await check_layout(page, ("generated display handoff", width, anchor))
            await screenshot(page, output, f"generated-{anchor}-{width}")
        for title in ("名称", "当前载体", "情况"):
            table = page.locator("#main .table-scroll").filter(
                has=page.get_by_role("columnheader", name=title, exact=True)
            )
            await expect(table).to_have_count(1)
            assert await table.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "Module meanings, implementation limits and fallback must be readable together"
            )
        for text in ("Run 继续指向 SkillVersion A", "冻结源码、依赖与构建配置"):
            diagram = page.locator("#main pre").filter(has_text=text)
            await expect(diagram).to_have_count(1)
            assert await diagram.evaluate("region => region.scrollWidth <= region.clientWidth"), (
                "Generated display flows should fit the mobile reading column"
            )

        await page.goto(page_url(book, "PJM/web/README.md", "生成表示と業務-module-を分ける"))
        await page.locator("#main").get_by_role("link", name="三つの概念", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_have_id("先分清三种模块与预览")
        await screenshot(page, output, f"generated-concepts-{width}")
        for anchor in (
            "当前-csp-常量不是投放配置",
            "构建输入与结果的提交",
            "挂载切换与晚到消息",
        ):
            await page.goto(page_url(book, "docs/design/generated-modules.md", anchor))
            await check_heading(page)
            await check_layout(page, ("generated safety boundary", width, anchor))
            await screenshot(page, output, f"generated-{anchor}-{width}")
        await page.locator("#main").get_by_role("link", name="契约入口", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_have_id(
            "生成-module-と既存-module-api-を分ける"
        )
        await screenshot(page, output, f"generated-contract-{width}")
        await (
            page.locator("#main")
            .get_by_role("link", name="内容 identity と歴史互換", exact=True)
            .click()
        )
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_id("内容身份与历史兼容")
        await screenshot(page, output, f"generated-identity-{width}")
        await page.locator("#main").get_by_role("link", name="R04", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h4:focus")).to_have_id("r04-生成模块")
        await check_layout(page, ("generated implementation task", width))
        await screenshot(page, output, f"generated-r04-{width}")


async def check_subagent_handoff(page: Page, book: Path, output: Path | None) -> None:
    """子結果の読み方から指令/出力・提交境界・実装へ実 link で進めることを確認する。"""

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "PJM/web/README.md", "子分析と用量を読む"))
    await page.get_by_role("link", name="一組の結果の読み方", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text(
        "一个例子：完成的是哪一层"  # noqa: RUF001
    )
    await screenshot(page, output, "subagent-outcomes-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "subagent outcomes mobile")
    await screenshot(page, output, "subagent-outcomes-mobile")

    await page.get_by_role("link", name="子任务的指令和输出", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("子任务指令与结果的边界")
    await check_layout(page, "subagent context mobile")
    await screenshot(page, output, "subagent-context-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.reload()
    await check_heading(page)
    await screenshot(page, output, "subagent-context-desktop")

    await page.get_by_role("link", name="保存与兼容", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("提交顺序与版本兼容")
    await screenshot(page, output, "subagent-commits-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "subagent commits mobile")
    commits = page.locator("#main pre").filter(has_text="保存整组子 Session")
    await expect(commits).to_have_count(1)
    assert await commits.evaluate("region => region.scrollWidth <= region.clientWidth"), (
        "The subagent commit flow must be readable without horizontal scrolling"
    )
    await screenshot(page, output, "subagent-commits-mobile")

    await page.get_by_role("link", name="开发接续", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("开发接续顺序")
    await page.get_by_role("link", name="Backend 接线", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("予算と子分析の接続を追う")
    await page.get_by_role("link", name="子指令と出力の責任", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("子任务指令与结果的边界")


async def check_stop_handoff(page: Page, book: Path, output: Path | None) -> None:
    """取消要求・業務終態・停止確認を、実装入口と旧章から同じ設計へ辿れることを守る。"""

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "PJM/backend/README.md", "実行の取消と停止を追う"))
    await page.get_by_role("link", name="取消後の四つの事実", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_contain_text("点击取消之后")
    await screenshot(page, output, "stop-facts-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "stop facts mobile")
    facts = page.locator("#main table").filter(has_text="看到的事实")
    await expect(facts).to_have_count(1)
    assert await facts.evaluate(
        "table => table.getBoundingClientRect().width <= table.parentElement.clientWidth + 1"
    ), "The short cancellation fact table must fit a mobile viewport"
    await screenshot(page, output, "stop-facts-mobile")

    await page.get_by_role("link", name="首事件前怎么办", exact=True).click()
    await check_heading(page)
    flow = page.locator("#main pre").filter(has_text="取消首事件等待任务")
    await expect(flow).to_have_count(1)
    assert await flow.evaluate("region => region.scrollWidth <= region.clientWidth"), (
        "The first-event cancellation flow must be readable without horizontal scrolling"
    )
    await screenshot(page, output, "stop-first-event-mobile")
    await page.get_by_role("link", name="怎样判定原因", exact=True).click()
    await check_heading(page)
    await check_layout(page, "stop reasons mobile")
    await screenshot(page, output, "stop-reasons-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.reload()
    await check_heading(page)
    await screenshot(page, output, "stop-reasons-desktop")

    await page.get_by_role("link", name="取消与完成谁先提交", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("提交时谁决定最终状态")
    await screenshot(page, output, "stop-commit-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "stop commit mobile")
    await screenshot(page, output, "stop-commit-mobile")
    await page.get_by_role("link", name="等待提交", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("等待提交仍是独立边界")
    waiting_flow = page.locator("#main pre").filter(has_text="拒绝新增待办或事件")
    await expect(waiting_flow).to_have_count(1)
    assert await waiting_flow.evaluate("region => region.scrollWidth <= region.clientWidth"), (
        "The two-transaction cancellation flow must fit a mobile viewport"
    )
    await screenshot(page, output, "stop-waiting-mobile")

    await page.get_by_role("link", name="终态之后还有什么", exact=True).click()
    await check_heading(page)
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "stop cleanup mobile")
    await screenshot(page, output, "stop-cleanup-mobile")
    await page.get_by_role("link", name="开发接续", exact=True).click()
    await check_heading(page)
    await page.get_by_role("link", name="Backend 监督接线", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("実行の取消と停止を追う")

    await page.goto(page_url(book, "PJM/web/README.md", "取消と最終状態を表示する"))
    await page.get_by_role("link", name="取消と終態の契約", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("Run の取消と終態を読む")
    await screenshot(page, output, "stop-contract-mobile")
    await page.get_by_role("link", name="提交の判断点", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("提交时谁决定最终状态")

    await page.goto(page_url(book, "PJM/web/README.md"))
    await page.get_by_role("link", name="取消後の事実", exact=True).click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_contain_text("点击取消之后")
    await page.goto(page_url(book, "docs/design/agent-runtime.md", "75-取消超时与失去执行权"))
    await page.get_by_role("link", name="执行监督与停止", exact=True).click()
    await expect(page.locator("#main h1")).to_have_text("执行监督与停止")


async def check_evidence_handoff(page: Page, book: Path, output: Path | None) -> None:
    """履歴のテーマ索引から証拠へ進み、現在の範囲表へ戻れることを確認する。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        await page.goto(page_url(book, "docs/README.md"))
        await page.get_by_role("link", name="履歴の索引", exact=True).click()
        await expect(page.locator("#main h1")).to_have_text("履歴の読み方と索引")
        await check_layout(page, ("history themes", width))
        for table in await page.locator("#main table").all():
            assert await table.evaluate(
                "t => t.getBoundingClientRect().width <= t.parentElement.clientWidth + 1"
            ), "History topics and scope must be visible without horizontal scrolling"
        await screenshot(page, output, f"history-themes-{width}")
        await page.get_by_role("link", name=re.compile("提交順序と現在の核対$")).click()
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_contain_text("70. 提交边界")
        await page.get_by_role("link", name="证据范围表", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_text("当前证据怎么用")
        await check_layout(page, ("evidence boundary", width))
        evidence = page.locator("#main table").filter(has_text="要判断什么")
        await expect(evidence).to_have_count(1)
        assert await evidence.evaluate(
            "table => table.getBoundingClientRect().width <= table.parentElement.clientWidth + 1"
        ), "Evidence and its limits must be visible together without horizontal scrolling"
        await screenshot(page, output, f"evidence-boundaries-{width}")


async def check_task_handoff(page: Page, book: Path, output: Path | None) -> None:
    """実装入口から独立した開発項目へ進み、状態・範囲・受入を同じ幅で読めることを守る。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        await page.goto(page_url(book, "PJM/backend/README.md", "実行の取消と停止を追う"))
        await page.get_by_role("link", name="計画 R07", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h4:focus")).to_have_text("R07 Run 与审计")
        # 旧章にも同じ参照があり得るため、開発項目の navigation 自体を検証対象にする。
        task_nav = page.locator("#main ul").filter(
            has=page.get_by_role("link", name=TASK_TARGETS[0][0], exact=True)
        )
        await expect(task_nav).to_have_count(1)
        for label, anchor in TASK_TARGETS:
            await task_nav.get_by_role("link", name=label, exact=True).click()
            await check_heading(page)
            await expect(page.locator("#main h4:focus")).to_have_attribute("id", anchor)
            await check_layout(page, ("task handoff", width, anchor))
            details = page.locator("#main h4:focus + ul")
            await expect(details.locator(":scope > li")).to_have_count(3)
            for index, prefix in enumerate(("状态", "范围", "验收")):
                await expect(details.locator(":scope > li").nth(index)).to_contain_text(
                    re.compile(f"^{prefix}")
                )
            assert await details.evaluate("list => list.scrollWidth <= list.clientWidth"), (
                "Task status, scope and acceptance must not require horizontal scrolling",
                anchor,
            )
            if anchor.startswith(("r02-", "r05-", "r07-", "r13-")):
                await screenshot(page, output, f"task-{anchor.split('-')[0]}-{width}")


async def check_benchmark_handoff(page: Page, book: Path, output: Path | None) -> None:
    """実行と評価の正本を分け、旧章・Skill README・検索から到達できることを守る。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        await page.goto(page_url(book, "docs/README.md"))
        await page.get_by_role("link", name="JAF 移行・運行受入", exact=True).click()
        await expect(page.locator("#main h1")).to_contain_text("迁移与端到端验收规范")
        await page.get_by_role("link", name="Benchmark 的执行/评价边界", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h2:focus")).to_have_text("先看一次评价的边界")
        await check_layout(page, ("benchmark flow", width))
        flow = page.locator("#main pre").first
        assert await flow.evaluate("node => node.scrollWidth <= node.clientWidth"), (
            "Execution/evaluation flow must remain visible without clipping"
        )
        await screenshot(page, output, f"benchmark-flow-{width}")

        await page.goto(page_url(book, "PJM/skills/README.md"))
        await page.get_by_role("link", name="評価データの境界", exact=True).click()
        await check_heading(page)
        await expect(page.locator("#main h3:focus")).to_have_text("哪些数据交给谁")
        boundary = page.locator("#main table").filter(has_text="Skill package")
        await expect(boundary).to_have_count(1)
        assert await boundary.evaluate(
            "table => table.getBoundingClientRect().width <= table.parentElement.clientWidth + 1"
        ), "Input and Gold boundaries must be visible together"
        await screenshot(page, output, f"benchmark-data-{width}")

        for old_anchor, target_anchor in (
            ("121-指标", "指标口径"),
            ("13-准确度报告", "报告内容"),
            ("17-评价数据模型目标", "记录对象与当前载体"),
        ):
            await page.goto(page_url(book, "docs/acceptance/jaf-quality.md", old_anchor))
            await check_heading(page)
            await page.locator("#main :is(h2,h3):focus + p a").first.click()
            await check_heading(page)
            await expect(page.locator("#main :is(h2,h3):focus")).to_have_id(target_anchor)
            await page.go_back()
            await check_heading(page)
            await expect(page.locator("#main :is(h2,h3):focus")).to_have_id(old_anchor)

        await page.goto(page_url(book, "docs/development/change-guide.md", "运行与数据"))
        for table in await page.locator("#main table").all():
            assert await table.evaluate(
                "node => node.getBoundingClientRect().width <= node.parentElement.clientWidth + 1"
            ), "Development paths must not separate purpose from its entry point"
        await screenshot(page, output, f"development-paths-{width}")
        for document, label, anchor in (
            ("PJM/backend/README.md", "生成 module", "生成-module-の前置実装を読む"),
            ("PJM/contracts/README.md", "認証", "認証と-secret-の契約を読む"),
        ):
            await page.goto(page_url(book, document))
            await page.locator("#main > p").get_by_role("link", name=label, exact=True).click()
            await check_heading(page)
            await expect(page.locator("#main h2:focus")).to_have_id(anchor)

    await page.locator("#search").fill("jaf-benchmark.md")
    first = page.locator("#navigation .search-hit > a").first
    assert (await first.get_attribute("href") or "").startswith(
        "#docs/acceptance/jaf-benchmark.md::"
    )
    await first.click()
    # 中国語の原見出しを検証するため、全角コロンをそのまま比較する。
    await expect(page.locator("#main h1")).to_have_text("JAF Benchmark：样本、评分与质量判断")  # noqa: RUF001
    await page.locator("#search").fill("")


async def check_operations_handoff(page: Page, book: Path, output: Path | None) -> None:
    """運用の目的別入口、旧章の往復と検索を確認し、本文 command は実行しない。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for document, label, anchor, heading in (
            ("docs/README.md", "公開・移行", "发布迁移与分阶段放行", "h1"),
            ("PJM/README.md", "backup・復元", "备份恢复与版本回退", "h1"),
            ("PJM/backend/README.md", "運用 CLI", "運用-cli-と停止境界を確認する", "h2"),
            ("PJM/images/README.md", "公開・移行", "发布迁移与分阶段放行", "h1"),
            ("docs/operations/quickstart.md", "公開・移行手順", "迁移前置与执行", "h2"),
        ):
            await page.goto(page_url(book, document))
            await page.locator("#main").get_by_role("link", name=label, exact=True).click()
            # 文書全体への移動は main、章への移動は見出しへ focus する契約を分ける。
            if heading == "h1":
                await expect(page.locator("#main")).to_be_focused()
                await expect(page.locator("#main h1")).to_have_id(anchor)
            else:
                await check_heading(page)
                await expect(page.locator("#main h2:focus")).to_have_id(anchor)
            await check_layout(page, ("operations entry", width, document))

        for old_anchor, target_document, target_anchor in (
            ("一致恢复点包含什么", "backup-recovery.md", "一致恢复点包含什么"),
            ("迁移与回退审查", "deployment.md", "迁移与回退审查"),
            ("会话协议切换检查", "deployment.md", "会话协议切换检查"),
            ("恢复前的停止条件", "backup-recovery.md", "恢复前的停止条件"),
            ("恢复后验证", "backup-recovery.md", "恢复后验证"),
            ("6-application-version-rollback", "backup-recovery.md", "应用版本回退"),
        ):
            await page.goto(page_url(book, "docs/operations/runbook.md", old_anchor))
            await check_heading(page)
            await page.locator("#main :is(h3,h4):focus + p a").first.click()
            await check_heading(page)
            await expect(page).to_have_url(
                page_url(book, f"docs/operations/{target_document}", target_anchor)
            )
            await expect(page.locator("#main :is(h2,h3):focus")).to_have_id(target_anchor)
            await page.go_back()
            await check_heading(page)
            await expect(page.locator("#main :is(h3,h4):focus")).to_have_id(old_anchor)
            await page.go_forward()
            await check_heading(page)
            await page.reload()
            await check_heading(page)

        for document, anchor, filename in (
            ("deployment.md", "一个例子关闭-dispatch-后仍有工作", "dispatch-boundary"),
            ("deployment.md", "启动与放行", "release-gates"),
            ("backup-recovery.md", "一个例子恢复不能抹掉后来的事实", "restore-timeline"),
            ("backup-recovery.md", "一致恢复点包含什么", "restore-point"),
            ("backup-recovery.md", "恢复后验证", "restore-acceptance"),
        ):
            await page.goto(page_url(book, f"docs/operations/{document}", anchor))
            await check_layout(page, ("operations boundary", width, anchor))
            await check_heading(page)
            if filename in {"dispatch-boundary", "restore-point"}:
                header = "实际入口" if filename == "dispatch-boundary" else "保存对象"
                table = page.locator("#main table").filter(
                    has=page.get_by_role("columnheader", name=header, exact=True)
                )
                await expect(table).to_have_count(1)
                assert await table.evaluate(
                    "table => table.getBoundingClientRect().width "
                    "<= table.parentElement.clientWidth + 1"
                ), (
                    "Operational facts and release limits must remain visible together",
                    width,
                    anchor,
                )
            await screenshot(page, output, f"operations-{filename}-{width}")

        for filename in ("deployment.md", "backup-recovery.md"):
            if not await page.locator("#search").is_visible():
                await page.get_by_role("button", name="目录", exact=True).click()
            await page.locator("#search").fill(filename)
            first = page.locator("#navigation .search-hit > a").first
            assert (await first.get_attribute("href") or "").startswith(
                f"#docs/operations/{filename}::"
            )
            await first.click()
            await expect(page.locator("#main")).to_be_focused()
            await expect(page).to_have_url(page_url(book, f"docs/operations/{filename}"))
            await check_layout(page, ("operations search", width, filename))
            # 窄幅では結果の選択が drawer を閉じるため、通常の操作で再び開く。
            if not await page.locator("#search").is_visible():
                await page.get_by_role("button", name="目录", exact=True).click()
            await page.locator("#search").fill("")


async def check(book: Path, output: Path | None) -> None:
    """全ページの layout と重要境界の章移動を検証し、外部接続の試行も失敗にする。"""

    errors: list[str] = []
    requests: list[str] = []

    async def deny_network(route: Route) -> None:
        """文書が CDN/API に依存していたら、到達前に拒否して検証を失敗させる。"""

        requests.append(route.request.url)
        await route.abort()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context()
            await context.route(re.compile(r"^https?://"), deny_network)
            page = await context.new_page()
            page.set_default_timeout(10_000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(book.as_uri())
            pages = await page.evaluate(
                'JSON.parse(document.getElementById("docs-data").textContent).pages'
                ".map(page => ({id:page.id,title:page.title,group:page.group}))"
            )
            assert pages, "empty documentation bundle"
            for width in (1440, 390):
                await page.set_viewport_size({"width": width, "height": 1000})
                for item in pages:
                    await page.goto(page_url(book, item["id"]))
                    await expect(page.locator("#main h1")).to_have_text(item["title"])
                    await check_layout(page, (width, item["id"]))
                    assert await page.locator(".archive-notice").count() == (
                        1 if item["group"] == "history" else 0
                    )
                print(f"{width}px: {len(pages)} pages passed", flush=True)

            for width in (320, 390, 768, 1440):
                await page.set_viewport_size({"width": width, "height": 1000})
                for size in ("16px", "24px"):
                    await page.evaluate("size => { document.body.style.fontSize = size; }", size)
                    for document, anchor in SECTION_TARGETS:
                        await page.goto(page_url(book, document, anchor))
                        await check_layout(page, (width, size, document, anchor))
                        await check_heading(page)
                        await page.reload()
                        # showPage を呼び直す前に native reload 自体の位置を検査する。
                        await check_layout(page, ("native reload", width, document, anchor))
                        await check_heading(page)
                        # reload が既定文字サイズへ戻すので、拡大した状態を再適用して再表示する。
                        await page.evaluate(
                            "size => { document.body.style.fontSize = size; showPage(); }", size
                        )
                        await check_layout(page, ("refresh", width, size, document))
                        await check_heading(page)
            await page.evaluate("document.body.style.fontSize = ''")
            await check_search_and_navigation(page, book, output)
            await check_budget_handoff(page, book, output)
            await check_budget_ledger_handoff(page, book, output)
            await check_auth_secret_handoff(page, book, output)
            await check_login_protection_handoff(page, book, output)
            await check_project_lifecycle_handoff(page, book, output)
            await check_document_lifecycle_handoff(page, book, output)
            await check_user_lifecycle_handoff(page, book, output)
            await check_contract_handoff(page, book, output)
            await check_interaction_handoff(page, book, output)
            await check_result_handoff(page, book, output)
            await check_generated_module_handoff(page, book, output)
            await check_subagent_handoff(page, book, output)
            await check_stop_handoff(page, book, output)
            await check_evidence_handoff(page, book, output)
            await check_task_handoff(page, book, output)
            await check_benchmark_handoff(page, book, output)
            await check_operations_handoff(page, book, output)
            assert not errors, errors
            assert not requests, requests
            print(
                json.dumps(
                    {
                        "pages": len(pages),
                        "page_layouts": len(pages) * 2,
                        "section_layouts": len(SECTION_TARGETS) * 8,
                        "native_reload_layouts": len(SECTION_TARGETS) * 8,
                        "refreshed_section_layouts": len(SECTION_TARGETS) * 8,
                        "navigation": "passed",
                        "javascript_errors": len(errors),
                        "external_requests": len(requests),
                    }
                ),
                flush=True,
            )
        finally:
            await browser.close()


def main() -> None:
    """既定の文書を読むだけで実行し、screenshot の保存は明示 option に限定する。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, default=ROOT / "docs/index.html")
    parser.add_argument(
        "--output", type=Path, help="Optional screenshot directory (files overwritten)"
    )
    args = parser.parse_args()
    book = args.book.resolve()
    if not book.is_file():
        parser.error(f"Documentation bundle not found: {book}; run scripts/build_docs.py first")
    output = args.output.resolve() if args.output else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    asyncio.run(check(book, output))


if __name__ == "__main__":
    main()
