/** Modal の通常 Tab 順を返し、portal popup・不可視 form input・focus guard を除外する。 */
export function modalTabStops(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(
    'button, a[href], input, select, textarea, summary, [tabindex="0"]',
  )).filter((element) => !element.matches(':disabled') && element.tabIndex >= 0
    && element.getAttribute('aria-hidden') !== 'true' && !element.closest('[data-select-popup]')
    && element.getClientRects().length > 0)
}
