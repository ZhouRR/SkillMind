import type { SkillDiagnostic, SkillParseResult, StoredSkillPreviewRecord } from '../api'
import { useMessages } from '../i18n'
import { formatByteSize } from '../lib/presentation'
import type { UploadedSourceFile } from '../lib/skillUpload'
import { DetailDrawer } from './PageElements'

/** 上传済み目录の内容を「Skill 源文件」module 内で確認する読取専用 preview。 */
export function UploadedSourceFiles({ files, onClear }: {
  files: UploadedSourceFile[]
  onClear: () => void
}) {
  const messages = useMessages()
  return (
    <div className="sourcePreview">
      <div className="sourcePreviewHeader">
        <span>{messages.skills.uploadedCount(files.length)}</span>
        <button className="secondaryButton compactButton" type="button" onClick={onClear}>{messages.skills.clearUseManual}</button>
      </div>
      <ul className="sourcePreviewList">
        {files.map((file) => (
          <li key={file.path}>
            {file.kind === 'text' ? (
              <details className="sourcePreviewFile" open={file.path.endsWith('SKILL.md')}>
                <summary><code>{file.path}</code><span>{formatByteSize(file.size)}</span></summary>
                <pre>{file.content}</pre>
              </details>
            ) : (
              <div className="sourcePreviewOpaque">
                <code>{file.path}</code>
                <span>
                  {file.kind === 'binary' ? messages.skills.binaryStored : messages.skills.textTooLarge}
                  {' · '}{formatByteSize(file.size)}
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Skill parser response から安全境界と draft identity を要約する。 */
export function SkillParseSummary({ result }: { result: SkillParseResult }) {
  const messages = useMessages()
  const normalized = result.normalized_package
  const manifest = result.runtime_manifest_draft
  // Package 診断は manifest compatibility 側へ複製されるため、単純結合すると同一診断が二重表示される。
  const diagnostics = dedupeDiagnostics([...normalized.diagnostics, ...manifest.compatibility.diagnostics])
  return (
    <div className="skillSummary">
      <dl className="runFacts">
        <div><dt>{messages.skills.parseNameLabel}</dt><dd>{normalized.metadata.name}</dd></div>
      </dl>
      <details className="technicalResultDetails">
        <summary>{messages.skills.technicalDetails}</summary>
        <dl className="runFacts">
        <div><dt>{messages.skills.parseAdapterLabel}</dt><dd>{normalized.source.detected_adapter}</dd></div>
        <div><dt>{messages.skills.parseSkillKeyLabel}</dt><dd className="mono">{manifest.identity.skill_key}</dd></div>
        <div><dt>{messages.skills.parseConfidenceLabel}</dt><dd>{manifest.compatibility.confidence}</dd></div>
        <div><dt>{messages.skills.parseToolsLabel}</dt><dd>{manifest.tools.length === 0 ? messages.skills.toolsUnauthorized : manifest.tools.length}</dd></div>
        </dl>
        {normalized.declared_tools.length > 0 && <p className="hint">{messages.skills.declaredToolsLine(normalized.declared_tools.join(', '))}</p>}
        {diagnostics.length > 0 && <ul className="diagnostics">{diagnostics.map((diagnostic, index) => <li key={`${diagnostic.code}-${index}`}><strong>{diagnostic.code}</strong><span>{diagnostic.message}</span></li>)}</ul>}
      </details>
      <div className="resourceGrid">
        <Metric label={messages.skills.metricFiles} value={normalized.source.files.length} />
        <Metric label={messages.skills.metricReferences} value={normalized.resources.references.length} />
        <Metric label={messages.skills.metricScripts} value={normalized.resources.scripts.length} />
        <Metric label={messages.skills.metricAssets} value={normalized.resources.assets.length} />
      </div>
    </div>
  )
}

/** 保存済み source と interpretation の不変 identity を表示する。 */
export function SavedSkillIdentity({ stored }: { stored: StoredSkillPreviewRecord }) {
  const messages = useMessages()
  return (
    <div className="savedSkill">
      <div className="savedSkillStatus"><span>{messages.skills.savedInterpretationStatus}</span><strong>{stored.interpretation_status}</strong></div>
      <DetailDrawer title={messages.elements.technicalDetails}>
        <dl className="runFacts">
          <div><dt>{messages.skills.savedSourceId}</dt><dd className="mono">{stored.skill_source_id}</dd></div>
          <div><dt>{messages.skills.savedInterpretationId}</dt><dd className="mono">{stored.interpretation_id}</dd></div>
        </dl>
      </DetailDrawer>
    </div>
  )
}

/** Small numeric metric を parser summary で揃えて表示する。 */
function Metric({ label, value }: { label: string; value: number }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>
}

/** 同一内容の診断を code・message・位置で一意化する。severity は同一 code 内で変わらない前提。 */
function dedupeDiagnostics(items: SkillDiagnostic[]): SkillDiagnostic[] {
  const seen = new Set<string>()
  return items.filter((item) => {
    const key = [item.code, item.message, item.path ?? '', item.line ?? ''].join('\u0000')
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

