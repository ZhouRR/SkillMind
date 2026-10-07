import { Children, isValidElement, type ComponentProps, type ReactNode } from 'react'
import { vi } from 'vitest'

import { Select } from '../../src/components/Select'

type SelectProps = ComponentProps<typeof Select>
interface OptionProps { children?: ReactNode; value?: string | number; disabled?: boolean }

/** 実 Select の SSR を保つ spy から、最後に渡された公開 props を読む。 */
export function lastSelectProps(): SelectProps {
  const props = vi.mocked(Select).mock.calls.at(-1)?.[0]
  if (!props) throw new Error('Expected an actual Select render')
  return props
}

/** 閉じた popup を SSR に強制せず、消費側が渡す候補の値・名称・無効状態を検証する。 */
export function selectOptions(props: SelectProps = lastSelectProps()): Array<{ value: string; label: string; disabled: boolean }> {
  /** option の表示文字列だけを抽出する。 */
  function label(node: ReactNode): string {
    return Children.toArray(node).map((child) => isValidElement<OptionProps>(child)
      ? label(child.props.children) : String(child)).join('')
  }
  /** map/fragment/optgroup を保持し、group の無効状態も候補へ引き継ぐ。 */
  function options(node: ReactNode, groupDisabled = false): ReturnType<typeof selectOptions> {
    return Children.toArray(node).flatMap((child) => {
      if (!isValidElement<OptionProps>(child)) return []
      if (child.type === 'option') return [{ value: String(child.props.value ?? label(child.props.children)),
        label: label(child.props.children), disabled: groupDisabled || Boolean(child.props.disabled) }]
      return options(child.props.children, groupDisabled || (child.type === 'optgroup' && Boolean(child.props.disabled)))
    })
  }
  return options(props.children)
}

/** 本物の SSR trigger だけを返し、閉じた候補の文字列と選択表示を混同しない。 */
export function comboboxes(html: string): string[] {
  return html.match(/<button\b[^>]*\brole="combobox"[^>]*>[\s\S]*?<\/button>/g) ?? []
}

/** 指定位置の trigger が存在しない場合は、空文字の否定照合で回帰を見逃さない。 */
export function combobox(html: string, index = 0): string {
  const trigger = comboboxes(html)[index]
  if (!trigger) throw new Error(`Expected combobox at index ${index}`)
  return trigger
}
