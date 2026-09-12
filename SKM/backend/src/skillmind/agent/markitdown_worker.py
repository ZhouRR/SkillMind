"""親 Worker が固定引数で起動する Excel 変換 process の入口。"""

from __future__ import annotations

import resource
import sys

from skillmind.agent.binary_text import MAX_EXCEL_INPUT_BYTES, render_excel_markdown


def _deny_external_io(event: str, arguments: tuple[object, ...]) -> None:
    """変換依存からの socket・子 process 起動を拒否する補助境界。"""

    if event.startswith("socket.") or event in {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty",
    }:
        raise PermissionError("External I/O is not available during Excel conversion")


def main() -> int:
    """CPU/メモリを制限し、stdin の原 bytes だけを Markdown に変換する。"""

    resource.setrlimit(resource.RLIMIT_AS, (2_147_483_648, 2_147_483_648))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    sys.addaudithook(_deny_external_io)
    if len(sys.argv) != 2 or sys.argv[1] not in {".xlsx", ".xls"}:
        return 1
    data = sys.stdin.buffer.read(MAX_EXCEL_INPUT_BYTES + 1)
    if not data or len(data) > MAX_EXCEL_INPUT_BYTES:
        return 1
    try:
        markdown = render_excel_markdown(sys.argv[1], data)
    except Exception:
        # Parser は多様な例外を返す。本文・内部 path を stderr や Tool error に漏らさない。
        return 1
    sys.stdout.buffer.write(markdown.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
