"""PyInstaller 入口脚本。

不能直接拿 ``slab/__main__.py`` 当入口——它用了包内相对导入（``from . import ...``），
PyInstaller 以「脚本」方式加载时会失败。这里做一层薄包装。
"""

from __future__ import annotations

import multiprocessing
import sys


def run() -> int:
    from slab.__main__ import main

    return main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run())
