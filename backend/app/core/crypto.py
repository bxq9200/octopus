"""
CryptoBox — 对 user_settings 里标记为 sensitive 的字段做对称加密

master.key 存在用户主目录 ~/.octopus/master.key（Windows: %USERPROFILE%\\.octopus\\master.key）
- 首次启动生成，之后复用
- 用户拷走 .db 文件但没有 master.key 时无法解密（.db 泄漏不等于 key 泄漏）
- 用户如果删了 master.key 就只能重新填 key（不可恢复）

明文 → encrypt → "enc:v1:<base64 ciphertext>"
解密时识别前缀，没前缀的值按明文处理（兼容兼容迁移过来的 .env 值）
"""

import base64
import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENCRYPTED_PREFIX = "enc:v1:"


def _master_key_path() -> Path:
    """跨平台地确定 master.key 路径。"""
    # 优先用 OCTOPUS_MASTER_KEY_FILE 显式覆盖（CI/测试有用）
    explicit = os.environ.get("OCTOPUS_MASTER_KEY_FILE")
    if explicit:
        return Path(explicit)
    home = Path.home()
    return home / ".octopus" / "master.key"


class CryptoBox:
    """单例懒加载：第一次用时从磁盘读 key，不存在就生成一个。"""

    _instance: "CryptoBox | None" = None

    @classmethod
    def instance(cls) -> "CryptoBox":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._fernet: Fernet | None = None
        self._load_or_create()

    def _load_or_create(self) -> None:
        key_file = _master_key_path()
        if key_file.exists():
            try:
                key = key_file.read_bytes().strip()
                # Fernet key 是 32 字节 url-safe base64 编码（44 字符）
                self._fernet = Fernet(key)
                logger.info("CryptoBox: 加载 master.key (%s)", key_file)
                return
            except Exception as e:
                logger.error("CryptoBox: master.key 损坏（%s），重新生成将丢失已加密数据", e)
                raise

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        # Unix 上设置 600 权限；Windows 的 ACL 另说（主目录本身就受保护）
        try:
            os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, NotImplementedError):
            pass
        self._fernet = Fernet(key)
        logger.info("CryptoBox: 生成新 master.key (%s)", key_file)

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            return ""
        if plaintext == "":
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return ENCRYPTED_PREFIX + token.decode("ascii")

    def decrypt(self, stored: str) -> str:
        """智能解密：有前缀 → Fernet；无前缀 → 当明文返回（兼容旧值）"""
        if not stored:
            return ""
        if not stored.startswith(ENCRYPTED_PREFIX):
            return stored  # 未加密，兼容从 .env 手动灌进来的明文
        token = stored[len(ENCRYPTED_PREFIX):].encode("ascii")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as e:
            logger.error("CryptoBox: 解密失败（master.key 与数据不匹配？）: %s", e)
            return ""


def encrypt_if_sensitive(value: str, sensitive: bool) -> str:
    if not sensitive or not value:
        return value or ""
    return CryptoBox.instance().encrypt(value)


def decrypt_if_needed(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(ENCRYPTED_PREFIX):
        return stored
    return CryptoBox.instance().decrypt(stored)
