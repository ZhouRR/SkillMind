import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { ActionMenu } from '../../src/components/ActionMenu'
import { ModalDialog } from '../../src/components/PageElements'
import { LanguageProvider } from '../../src/i18n'
import '../../src/styles/base.css'

/** Menu の DOM/focus/viewport だけを実 browser で観測する。API や永続化は呼ばない。 */
function Fixture() {
  const [owner, setOwner] = useState(1)
  const [disabled, setDisabled] = useState(false)
  const [mounted, setMounted] = useState(true)
  const [modal, setModal] = useState(false)
  const [selection, setSelection] = useState('')
  useEffect(() => {
    const changeOwner = () => setOwner((value) => value + 1)
    const disable = () => setDisabled((value) => !value)
    const unmount = () => setMounted(false)
    window.addEventListener('fixture-owner', changeOwner)
    window.addEventListener('fixture-disable', disable)
    window.addEventListener('fixture-unmount', unmount)
    return () => {
      window.removeEventListener('fixture-owner', changeOwner)
      window.removeEventListener('fixture-disable', disable)
      window.removeEventListener('fixture-unmount', unmount)
    }
  }, [])
  const label = '仕様書.md の操作 / 文档操作 / Document actions'
  return <>
    <button type="button" id="outside">Outside</button><output id="selection">{selection}</output>
    <div style={{ position: 'fixed', right: 8, bottom: 45, width: 'min(360px, calc(100vw - 16px))', height: 54, overflow: 'hidden', border: '1px solid var(--border)' }}>
      <details open><summary style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span>仕様書.md</span>
        {mounted && <ActionMenu label={label} ownerKey={`document-${owner}`} disabled={disabled} items={[
          { id: 'download', label: 'Download', href: './action-menu-download.txt', download: 'original.txt' },
          { id: 'blocked', label: 'Unavailable', disabled: true, onSelect: () => setSelection('blocked') },
          { id: 'rename', label: 'Rename', onSelect: () => { setSelection(document.activeElement?.getAttribute('aria-label') ?? 'wrong focus'); setModal(true) } },
          { id: 'long', label: '非常に長い操作名称でも表示領域内で折り返します / A long translated action remains readable in a narrow viewport', onSelect: () => setSelection('long') },
          { id: 'delete', label: 'Recycle', danger: true, separatorBefore: true, onSelect: () => setSelection('recycle') },
        ]} />}
      </summary></details>
    </div>
    <button type="button" id="after" style={{ position: 'fixed', right: 8, bottom: 4 }}>After row</button>
    <ModalDialog open={modal} title="Rename document" onClose={() => setModal(false)}><input aria-label="New name" defaultValue="仕様書.md" /></ModalDialog>
  </>
}

const root = document.getElementById('root')
if (!root) throw new Error('Fixture root missing')
createRoot(root).render(<LanguageProvider language="en"><Fixture /></LanguageProvider>)
