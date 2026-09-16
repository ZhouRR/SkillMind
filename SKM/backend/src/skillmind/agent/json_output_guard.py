"""JSON 文字列の内容を変えず、構造外の無意味な連続空白を検出する。"""

from __future__ import annotations

from dataclasses import dataclass

JSON_WHITESPACE_LIMIT = 4096


@dataclass
class JsonWhitespaceGuard:
    """分割 delta 間の quote/escape 状態と、構造外の連続空白数だけを保持する。"""

    consecutive: int = 0
    in_string: bool = False
    escaped: bool = False

    def exceeded(self, delta: str) -> bool:
        """JSON 本文を保存せず走査し、合法な string 内の長い空白を数えない。"""

        for character in delta:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.in_string = False
                continue
            if character in " \t\r\n":
                self.consecutive += 1
                if self.consecutive >= JSON_WHITESPACE_LIMIT:
                    return True
            else:
                self.consecutive = 0
                if character == '"':
                    self.in_string = True
        return False
