"""E2B 多 API Key 轮询池、账户归属非敏感指纹、备注别名解析与按 Key 并发锁管理。"""

import asyncio
import hashlib
import re
import time


class KeyInCooldownError(RuntimeError):
    """排队中的创建请求在轮到该 Key 时若发现已进入冷却，需直接跳过而不发送 RPC。"""


def _parse_key_entry(raw_entry):
    """解析单条 Key 配置，支持可选备注语法（如 `e2b_xxx # 主力号` 或 `主力号 # e2b_xxx`）。"""
    if not isinstance(raw_entry, str):
        return "", ""
    text = raw_entry.strip()
    if not text or text.startswith("#"):
        return "", ""
    if "#" not in text:
        return text, ""

    left, right = (part.strip() for part in text.split("#", 1))
    if not left or not right:
        return left or right, ""

    # 优先识别以 e2b_ 开头的标准密钥；否则遵循常规 `密钥 # 备注` 习惯（若左侧含空白/非 ASCII 而右侧紧凑无空白则反向识别）
    left_compact_ascii = left.isascii() and not any(ch.isspace() for ch in left)
    right_compact_ascii = right.isascii() and not any(ch.isspace() for ch in right)
    if right.startswith("e2b_") and not left.startswith("e2b_"):
        secret, alias = right, left
    elif left.startswith("e2b_") and not right.startswith("e2b_"):
        secret, alias = left, right
    elif not left_compact_ascii and right_compact_ascii:
        secret, alias = right, left
    else:
        secret, alias = left, right

    clean_alias = re.sub(r"[\x00-\x1f\x7f]", " ", alias).strip()[:40]
    return secret, clean_alias


class ApiKeyPool:
    """仅在新建沙箱时轮询选择账户；已有沙箱始终绑定创建时的原账户。"""

    def __init__(self, config):
        legacy_raw = config.get("e2b_api_key")
        legacy, legacy_alias = _parse_key_entry(legacy_raw) if isinstance(legacy_raw, str) else ("", "")
        raw = config.get("e2b_api_keys") or []
        if isinstance(raw, str):
            entries = re.split(r"[,\r\n]+", raw)
        elif isinstance(raw, (list, tuple)):
            entries = raw
        else:
            entries = []

        parsed_items = []
        if legacy:
            parsed_items.append((legacy, legacy_alias))
        for item in entries:
            secret, alias = _parse_key_entry(item)
            if secret:
                parsed_items.append((secret, alias))

        self._keys = {}
        self._aliases = {}
        self._order = []
        for value, alias in parsed_items:
            identifier = self.identifier(value)
            if identifier not in self._keys:
                self._keys[identifier] = value
                self._aliases[identifier] = alias
                self._order.append(identifier)
            elif alias and not self._aliases.get(identifier):
                self._aliases[identifier] = alias

        # 确保任何别名文本中不会意外混入已配置的密钥本体
        for identifier, alias in list(self._aliases.items()):
            if alias:
                self._aliases[identifier] = self.redact(alias)

        self.legacy_id = self.identifier(legacy) if legacy else ""
        self._cursor = 0
        self._lock = asyncio.Lock()
        self._creation_locks = {}
        self._cooldown_until = {}

    def alias(self, key_id):
        """返回配置中为该 Key 附加的非敏感可读备注（未设置时返回空字符串）。"""
        return self._aliases.get(key_id, "") if isinstance(key_id, str) else ""

    def creation_lock(self, key_id):
        """按账户维度隔离创建期 RPC 锁：同一 Key 串行防重复触发拒绝，不同 Key 可并行发请求。"""
        lock = self._creation_locks.get(key_id)
        if lock is None:
            lock = asyncio.Lock()
            self._creation_locks[key_id] = lock
        return lock

    def ensure_any_available(self):
        """准入预检：确认至少有一把 Key 未处于冷却，且不消耗轮询游标。"""
        if not self._order:
            raise RuntimeError("E2B API Key is missing.")
        now = time.monotonic()
        if not any(self._cooldown_until.get(key_id, 0) <= now for key_id in self._order):
            raise RuntimeError("All configured E2B API Keys are in cooldown.")

    @staticmethod
    def identifier(key):
        return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    @property
    def has_keys(self):
        return bool(self._order)

    def resolve(self, key_id=None):
        """解析已有沙箱绑定的密钥；严禁静默切换到其他账户以免串号报错。"""
        if key_id:
            if key_id not in self._keys:
                raise RuntimeError(
                    "The API Key bound to this sandbox is no longer configured. "
                    "Restore the original Key or explicitly destroy the sandbox "
                    "using its original account."
                )
            return self._keys[key_id]
        if self.legacy_id:
            return self._keys[self.legacy_id]
        if not self._order:
            raise RuntimeError("E2B API Key is missing.")
        raise RuntimeError(
            "This legacy sandbox has no recorded API Key owner. "
            "Restore the original e2b_api_key to manage it safely."
        )

    async def candidates(self):
        """按轮询顺序返回本次新建沙箱可用的候选 Key 列表（自动跳过冷却中的账户）。"""
        if not self._order:
            raise RuntimeError("E2B API Key is missing.")
        async with self._lock:
            start = self._cursor % len(self._order)
            ordered = self._order[start:] + self._order[:start]
            now = time.monotonic()
            active = [key_id for key_id in ordered
                      if self._cooldown_until.get(key_id, 0) <= now]
            if active:
                self._cursor = (self._order.index(active[0]) + 1) % len(self._order)
        if not active:
            raise RuntimeError("All configured E2B API Keys are in cooldown.")
        return [(key_id, self._keys[key_id]) for key_id in active]

    def ensure_available(self, key_id):
        """在容量排队、创建锁或限速等待结束后、真正发出 CREATE 前再次核对冷却状态。"""
        if self._cooldown_until.get(key_id, 0) > time.monotonic():
            raise KeyInCooldownError("This E2B API Key entered cooldown while waiting.")

    @staticmethod
    def is_definitive_account_rejection(exc, status):
        return status in (401, 402, 403, 429) or (
            type(exc).__name__ in ("AuthenticationException", "RateLimitException")
        )

    def redact(self, value):
        text = str(value)
        # 按密钥长度从长到短依次替换，避免短密钥先替换后暴露长密钥的后缀
        for secret in sorted(self._keys.values(), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def reject(self, key_id, status):
        # 对明确配额耗尽、鉴权失败或触发速率限制的账户执行临时冷却
        cooldown = 60 if status == 429 else 300
        self._cooldown_until[key_id] = time.monotonic() + cooldown
