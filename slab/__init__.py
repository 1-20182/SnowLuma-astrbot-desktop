"""SnowLuma × AstrBot 联合控制台。"""

from __future__ import annotations

# 尽早把 SSL 校验切到操作系统证书库：
# 本机（以及不少公司网络/带 TLS 检查的环境）certifi 内置根证书不全，
# 直接用 httpx 访问 GitHub 会报 CERTIFICATE_VERIFY_FAILED；truststore 走
# Windows CryptoAPI / macOS Keychain，能正常校验。必须在任何网络库使用前执行。
try:  # pragma: no cover - 环境相关
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - 没装 truststore 时保持默认行为
    pass

from .paths import APP_VERSION as __version__  # noqa: E402,F401
