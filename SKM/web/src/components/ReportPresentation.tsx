import { Component, type ReactNode } from 'react'
import { useMessages } from '../i18n'

/** 追加の report 排版が失敗しても、検証済み原結果をそのまま表示する。 */
export class ReportPresentation extends Component<{ value: unknown; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  /** 表示のみを切替え、再実行や外部 write を呼ばない。 */
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true }
  }

  render(): ReactNode {
    return this.state.failed
      ? <ReportFallback value={this.props.value} onRetry={() => this.setState({ failed: false })} />
      : this.props.children
  }
}

/** 理由を明示して元結果を保ち、再試行は同じデータの表示だけに限定する。 */
function ReportFallback({ value, onRetry }: { value: unknown; onRetry: () => void }) {
  const messages = useMessages()
  return <div className="reportFallback">
    <p role="status">{messages.uiAuditWorkspace.reportFallback}</p>
    <button className="secondaryButton compactButton" type="button" onClick={onRetry}>{messages.uiAuditWorkspace.reportRetry}</button>
    <pre className="rawResultBody" tabIndex={0} role="region" aria-label={messages.runResult.viewRawResult}>{JSON.stringify(value, null, 2)}</pre>
  </div>
}
