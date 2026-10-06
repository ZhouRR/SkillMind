/** Native picker が Escape を処理する間は親 dialog を閉じない。旧 browser は従来動作を保つ。 */
export function isOpenSelectPicker(target: EventTarget | null): boolean {
  if (!(target instanceof Element) || !globalThis.CSS?.supports('selector(select:open)')) return false
  return target.closest('select')?.matches(':open') ?? false
}
