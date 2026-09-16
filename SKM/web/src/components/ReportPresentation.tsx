import { Component, type ReactNode } from 'react'

/** 追加の report 排版が失敗しても、検証済み原結果をそのまま表示する。 */
export class ReportPresentation extends Component<{ value: unknown; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  /** 表示のみを切替え、再実行や外部 write を呼ばない。 */
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true }
  }

  render(): ReactNode {
    return this.state.failed ? <pre className="rawResultBody">{JSON.stringify(this.props.value, null, 2)}</pre> : this.props.children
  }
}
