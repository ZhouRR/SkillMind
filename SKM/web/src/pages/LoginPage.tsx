import { useEffect, useRef, useState, type FormEvent } from 'react'

import { login, type AuthSessionRecord } from '../api'
import { useMessages } from '../i18n'
import { loginFeedback } from '../lib/loginFeedback'
import { ThemeToggle } from '../components/ThemeToggle'
import { BrandMark } from '../components/BrandMark'

/** 初期表示の案内と request 失敗を分け、言語切替時には失敗を再翻訳する。 */
type LoginError = { message: string } | { reason: unknown }

/** Opaque session を開始し、password を component 外へ保持しない login 画面。 */
export function LoginPage({ onAuthenticated, initialError }: {
  onAuthenticated: (session: AuthSessionRecord) => void
  initialError?: string
}) {
  const messages = useMessages()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<LoginError | null>(initialError ? { message: initialError } : null)
  const activeRequest = useRef<AbortController | null>(null)
  const errorMessage = error === null ? null
    : 'reason' in error ? loginFeedback(error.reason, messages.login) : error.message

  useEffect(() => () => {
    activeRequest.current?.abort()
    activeRequest.current = null
  }, [])

  /** 同じ描画内の連続 submit も拒否し、離頁後の遅い結果を UI へ反映しない。 */
  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    if (activeRequest.current !== null) return
    const form = new FormData(event.currentTarget)
    const controller = new AbortController()
    activeRequest.current = controller
    setBusy(true)
    setError(null)
    try {
      const session = await login({
        email: String(form.get('email') ?? ''),
        password: String(form.get('password') ?? ''),
      }, controller.signal)
      if (!controller.signal.aborted && activeRequest.current === controller) onAuthenticated(session)
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError({ reason })
      }
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null
        if (!controller.signal.aborted) setBusy(false)
      }
    }
  }

  return (
    <main className="authShell">
      <div className="authAppearance"><ThemeToggle /></div>
      <div className="authLayout">
        <aside className="authStory">
          <div>
            <div className="authBrand">
              <BrandMark />
              <span><strong>Skillmind</strong><small>{messages.nav.brandTagline}</small></span>
            </div>
            <p className="authHeadline">{messages.login.introTitle}</p>
            <p className="authStoryDescription">{messages.login.introDescription}</p>
          </div>
          <ol className="authSteps">
            {[messages.routes.skills.label, messages.routes.resources.label, messages.routes.workspace.label].map((label, index) => (
              <li key={label}><span className="stepNumber" aria-hidden="true">0{index + 1}</span>{label}</li>
            ))}
          </ol>
        </aside>
        <section className="authCard">
          <div><h1>{messages.login.title}</h1><p>{messages.login.subtitle}</p></div>
          <form aria-busy={busy} onSubmit={(event) => void submit(event)}>
            <label>
              {messages.login.email}
              <input autoComplete="username" autoFocus name="email" required type="email" />
            </label>
            <label>
              {messages.login.password}
              <input autoComplete="current-password" name="password" required type="password" />
            </label>
            {errorMessage && <p className="error" role="alert">{errorMessage}</p>}
            <button className="primaryButton" disabled={busy} type="submit">
              {busy ? messages.login.submitting : messages.login.submit}
            </button>
          </form>
          <p className="authFooter">{messages.login.footer}</p>
        </section>
      </div>
    </main>
  )
}
