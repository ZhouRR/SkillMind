"""共有 popup の scrollbar、候補内余白と選択表示の実レイアウトを検証する。"""
from __future__ import annotations

from playwright.async_api import Locator


async def check_popup_geometry(popup: Locator, *, indicators: bool, scrollable: bool | None = None) -> dict:
    """CSS の宣言値だけに頼らず、候補と本文・checkmark の矩形で左右対称と幅を確認する。"""
    geometry = await popup.evaluate('''async popup => {
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      const bounds = popup.getBoundingClientRect();
      const style = getComputedStyle(popup);
      return {
        clientWidth: popup.clientWidth, scrollWidth: popup.scrollWidth,
        clientHeight: popup.clientHeight, scrollHeight: popup.scrollHeight,
        reservedInlineSpace: bounds.width - popup.clientWidth
          - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth),
        items: Array.from(popup.querySelectorAll('[role="option"]')).map(item => {
          const rect = item.getBoundingClientRect();
          const itemStyle = getComputedStyle(item);
          const text = item.querySelector('.selectItemText');
          const textRect = text.getBoundingClientRect();
          const indicator = item.querySelector('.selectItemIndicator');
          const indicatorRect = indicator?.getBoundingClientRect();
          return {
            value: item.getAttribute('data-value') ?? item.getAttribute('data-folder-path'),
            selected: item.getAttribute('aria-selected') === 'true',
            width: rect.width, leftInset: rect.left - bounds.left, rightInset: bounds.right - rect.right,
            paddingLeft: parseFloat(itemStyle.paddingLeft), paddingRight: parseFloat(itemStyle.paddingRight),
            contentLeft: textRect.left - rect.left - parseFloat(itemStyle.borderLeftWidth),
            contentRight: rect.right - (indicatorRect?.right ?? textRect.right) - parseFloat(itemStyle.borderRightWidth),
            textWidth: textRect.width, textOverflow: text.scrollWidth - text.clientWidth,
            itemOverflow: item.scrollWidth - item.clientWidth,
            indicatorWidth: indicatorRect?.width ?? null,
            indicatorGap: indicatorRect ? indicatorRect.left - textRect.right : null,
            indicatorVisibility: indicator ? getComputedStyle(indicator).visibility : null,
          };
        }),
      };
    }''')
    tolerance = 1
    items = geometry['items']
    assert items, geometry
    assert geometry['clientWidth'] > 0 and geometry['clientHeight'] > 0, geometry
    assert geometry['scrollWidth'] <= geometry['clientWidth'] + tolerance, geometry
    overflowing = geometry['scrollHeight'] > geometry['clientHeight'] + tolerance
    if scrollable is not None:
        assert overflowing == scrollable, geometry
    first = items[0]
    for item in items:
        assert item['leftInset'] > 0 and item['rightInset'] > 0, item
        assert abs(item['leftInset'] - item['rightInset']) <= tolerance, item
        assert abs(item['paddingLeft'] - item['paddingRight']) <= tolerance, item
        assert item['contentLeft'] > 0 and item['contentRight'] > 0, item
        assert abs(item['contentLeft'] - item['contentRight']) <= tolerance, item
        assert abs(item['contentLeft'] - item['paddingLeft']) <= tolerance, item
        assert abs(item['contentRight'] - item['paddingRight']) <= tolerance, item
        assert item['textWidth'] > 0 and item['textOverflow'] <= tolerance and item['itemOverflow'] <= tolerance, item
        # group、root、選択状態が変わっても同じ候補幅と本文の開始位置を維持する。
        for metric in ('width', 'leftInset', 'rightInset', 'contentLeft', 'contentRight', 'textWidth'):
            assert abs(item[metric] - first[metric]) <= tolerance, {'metric': metric, 'first': first, 'item': item}
        if indicators:
            assert item['indicatorWidth'] is not None and item['indicatorWidth'] > 0, item
            assert item['indicatorGap'] is not None and item['indicatorGap'] >= -tolerance, item
            assert abs(item['indicatorWidth'] - first['indicatorWidth']) <= tolerance, item
            assert abs(item['indicatorGap'] - first['indicatorGap']) <= tolerance, item
            assert item['indicatorVisibility'] == ('visible' if item['selected'] else 'hidden'), item
        else:
            assert item['indicatorWidth'] is None, item
    if indicators:
        assert any(item['selected'] for item in items) and any(not item['selected'] for item in items), items
    return geometry
