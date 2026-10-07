import { Autocomplete } from '@base-ui/react/autocomplete'
import { useEffect, useId, useMemo, useState } from 'react'

import { useMessages } from '../i18n'

/** 既存フォルダーの候補と自由入力を同じ文字列契約で扱う。空文字は root。 */
interface DocumentFolderInputProps {
  value: string
  folders: Iterable<string>
  disabled: boolean
  onValueChange: (value: string) => void
}

/** OS の datalist popup に依存せず、新規パスを保持する upload 先入力。 */
export function DocumentFolderInput({ value, folders, disabled, onValueChange }: DocumentFolderInputProps) {
  const messages = useMessages()
  const id = useId()
  const [open, setOpen] = useState(false)
  // upload/権限変更でロックした候補を閉じ、解除時に勝手に再展開しない。
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])
  // API が空の子フォルダーだけを返す場合も親を候補に含める。path 自体は変更しない。
  const paths = new Set<string>([''])
  for (const folder of folders) {
    if (!folder) continue
    paths.add(folder)
    let parent = folder.lastIndexOf('/')
    while (parent > 0) { paths.add(folder.slice(0, parent)); parent = folder.lastIndexOf('/', parent - 1) }
  }
  const signature = JSON.stringify([...paths].sort())
  // 親の進捗更新や入力で同一候補を再登録し、popup の active item を揺らさない。
  const items = useMemo(() => JSON.parse(signature) as string[], [signature])

  return <div className="documentTargetFolder">
    <label id={`${id}-label`} htmlFor={id}>{messages.documentsPanel.targetFolder}</label>
    <Autocomplete.Root items={items} value={value} disabled={disabled} open={open && !disabled} openOnInputClick
      onOpenChange={(next, details) => {
        if (next && disabled) { details.cancel(); return }
        setOpen(next)
      }}
      onValueChange={(next, details) => {
        // Escape は候補を閉じるだけ。upload 先を意図せず root に戻さない。
        if (disabled || details.reason === 'escape-key') { details.cancel(); return }
        onValueChange(next)
      }}
      filter={(path, query) => path === '' || path.toLocaleLowerCase().includes(query.toLocaleLowerCase())}>
      <Autocomplete.InputGroup className="documentFolderInputGroup">
        <Autocomplete.Input id={id} maxLength={200} placeholder={messages.documentsPanel.rootFolder} autoComplete="off" />
        <Autocomplete.Trigger className="documentFolderTrigger" aria-label={messages.documentsPanel.targetFolder}>
          <Autocomplete.Icon className="selectIcon"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg></Autocomplete.Icon>
        </Autocomplete.Trigger>
      </Autocomplete.InputGroup>
      <Autocomplete.Portal>
        <Autocomplete.Positioner className="selectPositioner" positionMethod="fixed" sideOffset={6} collisionPadding={10} align="start">
          <Autocomplete.Popup className="selectPopup documentFolderPopup">
            <Autocomplete.List aria-labelledby={`${id}-label`}>
              {(path: string) => <Autocomplete.Item key={path} value={path} className="selectItem" data-folder-path={path}>
                <span className="selectItemText">{path || messages.documentsPanel.rootFolder}</span>
              </Autocomplete.Item>}
            </Autocomplete.List>
          </Autocomplete.Popup>
        </Autocomplete.Positioner>
      </Autocomplete.Portal>
    </Autocomplete.Root>
  </div>
}
