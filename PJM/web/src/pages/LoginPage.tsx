import { useEffect, useRef, useState, type FormEvent } from 'react'

import { login, type AuthSessionRecord } from '../api'
import { useMessages } from '../i18n'

/** Opaque session を開始し、password を component 外へ保持しない login 画面。 */
export function LoginPage({ onAuthenticated, initialError }: {
  onAuthenticated: (session: AuthSessionRecord) => void
  initialError?: string
}) {
  const messages = useMessages()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(initialError ?? null)
  const controllers = useRef<Set<AbortController>>(new Set())

  useEffect(() => () => {
    for (const controller of controllers.current) controller.abort()
    controllers.current.clear()
  }, [])

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const controller = new AbortController()
    controllers.current.add(controller)
    setBusy(true)
    setError(null)
    try {
      const session = await login({
        email: String(form.get('email') ?? ''),
        password: String(form.get('password') ?? ''),
      }, controller.signal)
      onAuthenticated(session)
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(reason instanceof Error ? reason.message : messages.login.failed)
      }
    } finally {
      controllers.current.delete(controller)
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  return (
    <main className="authShell">
      <section className="authCard">
        <div className="authBrand">
          <span className="brandMark">PM</span>
          <span><strong>ProjectMind</strong><small>{messages.nav.brandTagline}</small></span>
        </div>
        <div><h1>{messages.login.title}</h1><p>{messages.login.subtitle}</p></div>
        <form onSubmit={(event) => void submit(event)}>
          <label>
            {messages.login.email}
            <input autoComplete="username" autoFocus name="email" required type="email" />
          </label>
          <label>
            {messages.login.password}
            <input autoComplete="current-password" name="password" required type="password" />
          </label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primaryButton" disabled={busy} type="submit">
            {busy ? messages.login.submitting : messages.login.submit}
          </button>
        </form>
        <p className="authFooter">{messages.login.footer}</p>
      </section>
    </main>
  )
}
