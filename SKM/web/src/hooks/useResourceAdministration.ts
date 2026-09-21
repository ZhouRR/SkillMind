import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import {
  loadEffectPreauthorizations, loadIntegrations, loadProjectTasks, loadResourceBindings,
  loadSecretReferences, type EffectPreauthorizationRecord, type IntegrationRecord,
  type PublishedTaskRecord, type ResourceBindingRecord, type SecretReferenceRecord,
} from '../api'
import { useMessages } from '../i18n'
import { apiErrorMessage } from '../lib/apiFeedback'

/** 同じ Project と読取世代に属する管理一覧。補助 task の失敗は一覧全体を閉じない。 */
interface ResourceCatalog {
  secrets: SecretReferenceRecord[]
  integrations: IntegrationRecord[]
  bindings: ResourceBindingRecord[]
  policies: EffectPreauthorizationRecord[]
  tasks: PublishedTaskRecord[]
}

const EMPTY_CATALOG: ResourceCatalog = { secrets: [], integrations: [], bindings: [], policies: [], tasks: [] }

/** 管理画面の読取と単一操作の所有権を持つ。Abort は書込の取消確認として扱わない。 */
export function useResourceAdministration(projectId: string, deferredFeaturesEnabled: boolean) {
  const messages = useMessages()
  const [catalog, setCatalog] = useState({ projectId, value: EMPTY_CATALOG })
  const [revision, setRevision] = useState(0)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const loadRequest = useRef<AbortController | null>(null)
  const actionRequest = useRef<AbortController | null>(null)
  const owner = useRef(projectId)
  owner.current = projectId
  const mounted = useRef(false)

  useLayoutEffect(() => {
    mounted.current = true
    setBusy(null)
    setError(null)
    return () => {
      mounted.current = false
      loadRequest.current?.abort()
      actionRequest.current?.abort()
      actionRequest.current = null
    }
  }, [projectId])

  useEffect(() => {
    if (!projectId) { setLoading(false); return }
    const controller = new AbortController()
    loadRequest.current = controller
    setLoading(true)
    setError(null)
    const current = (): boolean => mounted.current && owner.current === projectId
      && loadRequest.current === controller && !controller.signal.aborted
    void Promise.all([
      loadSecretReferences(projectId, controller.signal),
      loadIntegrations(projectId, controller.signal),
      loadResourceBindings(projectId, controller.signal),
      deferredFeaturesEnabled ? loadEffectPreauthorizations(projectId, controller.signal) : Promise.resolve([]),
      loadProjectTasks(projectId, controller.signal).then((value) => value.tasks)
        .catch(() => [] as PublishedTaskRecord[]),
    ]).then(([secrets, integrations, bindings, policies, tasks]) => {
      if (current()) setCatalog({ projectId, value: { secrets, integrations, bindings, policies, tasks } })
    }).catch((caught: unknown) => {
      if (current()) setError(apiErrorMessage(caught, messages.resources.loadFailed, messages))
    }).finally(() => {
      if (current()) setLoading(false)
    })
    return () => controller.abort()
  }, [projectId, revision, deferredFeaturesEnabled])

  /** 未完了の操作を置き換えず、同一 event loop 内の二重送信も同期的に拒否する。 */
  async function perform(
    key: string, action: (signal: AbortSignal) => Promise<unknown>, kind: 'read' | 'write' = 'write',
  ): Promise<boolean> {
    if (!mounted.current || owner.current !== projectId || actionRequest.current !== null) return false
    const controller = new AbortController()
    actionRequest.current = controller
    const current = (): boolean => mounted.current && owner.current === projectId
      && actionRequest.current === controller && !controller.signal.aborted
    setBusy(key)
    setError(null)
    try {
      await action(controller.signal)
      if (!current()) return false
      if (kind === 'write') setRevision((value) => value + 1)
      return true
    } catch (caught: unknown) {
      // 通信失敗を未送信・rollback と解釈せず、再送は一切行わない。
      if (current()) setError(apiErrorMessage(caught, kind === 'read' ? messages.resources.loadFailed : messages.resources.mutationFailed, messages))
      return false
    } finally {
      if (current()) { actionRequest.current = null; setBusy(null) }
    }
  }

  /** 接続保存の中間成功を保持する。後段失敗時も同じ Secret を再利用できる。 */
  function recordSecret(secret: SecretReferenceRecord): void {
    if (!mounted.current || owner.current !== projectId || secret.project_id !== projectId) return
    setCatalog((current) => current.projectId !== projectId ? current : ({ ...current,
      value: { ...current.value, secrets: [...current.value.secrets.filter(
        (item) => item.secret_reference_id !== secret.secret_reference_id), secret] },
    }))
  }

  return { ...(catalog.projectId === projectId ? catalog.value : EMPTY_CATALOG), loading,
    busy, error, setError, perform, recordSecret }
}
