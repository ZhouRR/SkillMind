// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { ResourceConnectionForm } from '../../src/components/ResourceConnectionForm'
import { emptyConnectDraft, type ConnectDraft } from '../../src/lib/resourceDrafts'
import { prepareResourceConnection } from '../../src/lib/resourceConnection'
import { MESSAGES } from '../../src/lib/i18n/messages'

/** 実 form の根 URL 追加・削除・送信を確認し、旧 scope 欄を出さない。 */
it('adds and removes HTTP roots and submits only roots and authentication settings', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)
  let saved: ReturnType<typeof prepareResourceConnection> | undefined
  const labels = MESSAGES.zh.resources
  /** 通信を持たない form owner。保存値だけを捕捉する。 */
  function Harness() {
    const [draft, setDraft] = useState<ConnectDraft>({ ...emptyConnectDraft('http'),
      name: 'Webhook', httpAuthMode: 'none', httpBaseUrls: ['https://first.example.test/api'],
      access: 'read_write', credentialChoice: '' })
    return <ResourceConnectionForm connectDraft={draft} setConnectDraft={setDraft}
      secrets={[]} editingIntegration={null} connectWriteEnabled mcpToolsEnabled={false}
      busy={null} error={null} locked={false} feedback={null} onClose={() => {}}
      discoverTools={async () => {}} submitConnect={async (event) => {
        event.preventDefault(); saved = prepareResourceConnection(draft, true)
      }} />
  }
  const click = async (text: string) => {
    const button = [...container.querySelectorAll('button')].find((item) => item.textContent === text)
    if (!button) throw new Error(`Missing button: ${text}`)
    await act(async () => button.click())
  }
  try {
    await act(async () => root.render(<Harness />))
    expect(container.textContent).not.toContain(MESSAGES.zh.resourcesAudit.scopeSection)
    await click(labels.httpAddAddress)
    const inputs = container.querySelectorAll<HTMLInputElement>('.httpAddressRow input')
    expect(inputs).toHaveLength(2)
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(
        inputs[1], 'https://second.example.test/api',
      )
      inputs[1]!.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click(labels.httpRemoveAddress)
    expect(container.querySelectorAll('.httpAddressRow input')).toHaveLength(1)
    await act(async () => container.querySelector('form')!.dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    ))
    expect(saved).toMatchObject({ valid: true, input: {
      scope: { base_urls: ['https://second.example.test/api'] }, config: { auth_mode: 'none' },
      capabilities: ['http.read/v1', 'http.write/v1'],
    } })
  } finally {
    await act(async () => root.unmount())
    container.remove(); vi.unstubAllGlobals()
  }
})
