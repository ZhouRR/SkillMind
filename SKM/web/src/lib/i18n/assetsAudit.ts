/** 文書と Skill の状態・回復案内。共通 catalog へ三語同時に組み込む。 */
export interface AssetsAuditMessages {
  sourceChanged: string; previousInterpretation: string; enablementUnknown: string
  parserFailed: string; saveFailed: string; uploadFailed: string; draftFailed: string; publishFailed: string
  diagnosticsSummary: (errors: number, warnings: number) => string; diagnosticsNext: string
  previewAvailable: string; downloadUnsupported: string; downloadOversized: string
  completedUploads: (count: number) => string; uploadProgress: (settled: number, total: number) => string
  fullPath: string; sourceRegion: string; imageOriginal: string; imageFit: string; frameNavigation: string
  stagesLabel: string; stages: { parse: string; save: string; interpret: string; publish: string }
  severity: Record<string, string>; compatibility: Record<string, string>; interpretation: Record<string, string>
  dimensions: Record<string, string>; unknown: string
}

export const ASSETS_AUDIT_ZH: AssetsAuditMessages = {
  sourceChanged: '源文已更改，请重新解析后保存。之前的结果不适用于当前输入。',
  previousInterpretation: '这是之前源文的解释结果。请先核对原请求；当前输入需重新解析和保存。',
  enablementUnknown: '项目启用状态待确认',
  parserFailed: '解析失败，请检查源文后重试。', saveFailed: '保存失败，请核对源文后重试。',
  uploadFailed: '上传失败，请检查文件后重试。', draftFailed: '创建草稿失败，请核对解释结果后重试。',
  publishFailed: '发布失败，请核对版本状态后重试。',
  diagnosticsSummary: (errors, warnings) => `${errors} 项错误 · ${warnings} 项警告`,
  diagnosticsNext: '请检查以下问题，修改源文后重新解析；发布前仍会执行版本校验。',
  previewAvailable: '可预览', downloadUnsupported: '仅下载 · 暂不支持此格式预览', downloadOversized: '仅下载 · 超过 20 MB 预览上限',
  completedUploads: (count) => `已结束的上传（${count} 项）`, uploadProgress: (settled, total) => `已确认 ${settled} / ${total} 项`,
  fullPath: '完整目录路径', sourceRegion: '源文阅读区域', imageOriginal: '原始尺寸', imageFit: '适合窗口',
  frameNavigation: '预览内容位于独立阅读框。使用 Tab 或 Shift+Tab 可返回外部的关闭和下载操作。',
  stagesLabel: '技能导入进度', stages: { parse: '解析源文', save: '保存源文', interpret: '解释与草稿', publish: '检查并发布' },
  severity: { error: '错误', warning: '警告', info: '信息' },
  compatibility: { native: '原生兼容', adapted: '适配后兼容', assisted: '需辅助适配', unsupported: '暂不支持', incompatible: '不兼容', source: '原文执行' },
  interpretation: { ANALYZING: '正在分析', SUPERSEDED: '已有新解释', PREVIEW_READY: '预览已就绪', READY: '已就绪', FAILED: '解释失败', PENDING: '等待解释', RUNNING: '正在解释', DRAFT: '草稿', ASSISTED: '辅助解析', PARSED: '已解析' },
  dimensions: { capabilities: '能力', tasks: '任务', data_sources: '数据源', tools: '工具', workflows: '流程', identity: '身份信息', permissions: '权限', ui: '界面', confidence: '置信度', skill_execution: '技能执行', diagnostics: '诊断', compatibility_level: '兼容性', resources: '资源', schemas: '数据结构' }, unknown: '未识别状态',
}

export const ASSETS_AUDIT_JA: AssetsAuditMessages = {
  sourceChanged: '原文が変更されました。解析し直してから保存してください。以前の結果は現在の入力には適用されません。',
  previousInterpretation: '以前の原文の解釈結果です。元の要求を確認してください。現在の入力は再解析と保存が必要です。',
  enablementUnknown: 'プロジェクトの有効状態は未確認',
  parserFailed: '解析できませんでした。原文を確認して再試行してください。', saveFailed: '保存できませんでした。原文を確認して再試行してください。',
  uploadFailed: 'アップロードできませんでした。ファイルを確認して再試行してください。', draftFailed: '下書きを作成できませんでした。解釈結果を確認して再試行してください。', publishFailed: '公開できませんでした。版の状態を確認して再試行してください。',
  diagnosticsSummary: (errors, warnings) => `エラー ${errors} 件・警告 ${warnings} 件`, diagnosticsNext: '以下を確認し、原文を修正して再解析してください。公開前にも版の検証を行います。',
  previewAvailable: 'プレビュー可能', downloadUnsupported: 'ダウンロードのみ・未対応の形式', downloadOversized: 'ダウンロードのみ・20 MB のプレビュー上限を超過',
  completedUploads: (count) => `終了したアップロード（${count} 件）`, uploadProgress: (settled, total) => `${total} 件中 ${settled} 件を確認済み`,
  fullPath: 'フォルダーの完全パス', sourceRegion: '原文の閲覧領域', imageOriginal: '原寸表示', imageFit: '画面に合わせる',
  frameNavigation: 'プレビューは独立した閲覧枠にあります。Tab または Shift+Tab で外側の閉じる・ダウンロード操作へ戻れます。',
  stagesLabel: 'スキルの取込状況', stages: { parse: '原文を解析', save: '原文を保存', interpret: '解釈と下書き', publish: '確認して公開' },
  severity: { error: 'エラー', warning: '警告', info: '情報' },
  compatibility: { native: '標準互換', adapted: '適応済み', assisted: '補助が必要', unsupported: '未対応', incompatible: '非互換', source: '原文実行' },
  interpretation: { ANALYZING: '分析中', SUPERSEDED: '新しい解釈あり', PREVIEW_READY: 'プレビュー準備完了', READY: '準備完了', FAILED: '解釈に失敗', PENDING: '解釈待ち', RUNNING: '解釈中', DRAFT: '下書き', ASSISTED: '補助解析', PARSED: '解析済み' },
  dimensions: { capabilities: '能力', tasks: 'タスク', data_sources: 'データソース', tools: 'ツール', workflows: 'フロー', identity: '識別情報', permissions: '権限', ui: '画面', confidence: '確信度', skill_execution: 'スキル実行', diagnostics: '診断', compatibility_level: '互換性', resources: 'リソース', schemas: 'データ構造' }, unknown: '未認識の状態',
}

export const ASSETS_AUDIT_EN: AssetsAuditMessages = {
  sourceChanged: 'The source has changed. Parse it again before saving. Earlier results do not describe the current input.',
  previousInterpretation: 'This interpretation belongs to an earlier source. Check the original request; parse and save the current input separately.',
  enablementUnknown: 'Project enablement is unconfirmed', parserFailed: 'Parsing failed. Check the source and try again.', saveFailed: 'Saving failed. Check the source and try again.',
  uploadFailed: 'Upload failed. Check the files and try again.', draftFailed: 'Draft creation failed. Review the interpretation and try again.', publishFailed: 'Publishing failed. Check the version status and try again.',
  diagnosticsSummary: (errors, warnings) => `${errors} errors · ${warnings} warnings`, diagnosticsNext: 'Review these issues, then edit and parse the source again. Version checks will still run before publication.',
  previewAvailable: 'Preview available', downloadUnsupported: 'Download only · Preview format unsupported', downloadOversized: 'Download only · Exceeds the 20 MB preview limit',
  completedUploads: (count) => `Finished uploads (${count})`, uploadProgress: (settled, total) => `${settled} of ${total} confirmed`,
  fullPath: 'Full folder path', sourceRegion: 'Source reading area', imageOriginal: 'Original size', imageFit: 'Fit to window',
  frameNavigation: 'The preview is a separate reading frame. Use Tab or Shift+Tab to return to the outer close and download controls.',
  stagesLabel: 'Skill import progress', stages: { parse: 'Parse source', save: 'Save source', interpret: 'Interpret and draft', publish: 'Review and publish' },
  severity: { error: 'Error', warning: 'Warning', info: 'Information' },
  compatibility: { native: 'Native compatibility', adapted: 'Adapted compatibility', assisted: 'Assistance required', unsupported: 'Unsupported', incompatible: 'Incompatible', source: 'Source execution' },
  interpretation: { ANALYZING: 'Analyzing', SUPERSEDED: 'Superseded by a newer interpretation', PREVIEW_READY: 'Preview ready', READY: 'Ready', FAILED: 'Interpretation failed', PENDING: 'Awaiting interpretation', RUNNING: 'Interpreting', DRAFT: 'Draft', ASSISTED: 'Assisted parsing', PARSED: 'Parsed' },
  dimensions: { capabilities: 'Capabilities', tasks: 'Tasks', data_sources: 'Data sources', tools: 'Tools', workflows: 'Workflows', identity: 'Identity', permissions: 'Permissions', ui: 'Interface', confidence: 'Confidence', skill_execution: 'Skill execution', diagnostics: 'Diagnostics', compatibility_level: 'Compatibility', resources: 'Resources', schemas: 'Schemas' }, unknown: 'Unrecognized state',
}

/** 未知の契約値も原 key を失わず、単なる状態名として誤認させない。 */
export function assetCodeLabel(labels: Record<string, string>, key: string, unknown: string): string {
  return labels[key] ?? `${unknown} (${key})`
}
