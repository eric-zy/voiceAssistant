"""Credential normalization and safe diagnostics; never print credentials."""
import hashlib


def normalize_key(raw: str) -> str:
    key = raw.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
        key = key[1:-1].strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else "unset"


def auth_error(status: int) -> str:
    if status == 401:
        return "认证失败（HTTP 401）：当前接口拒绝了 Key。请使用 ./start.ps1 -UpdateKey 重新输入并验证；检查 Key 是否属于该接口平台。"
    if status == 402:
        return "账户余额不足（HTTP 402），请检查 DeepSeek API 账户余额。"
    if status == 429:
        return "请求受限（HTTP 429），请稍后重试。"
    if status in {502, 503, 504}:
        return f"DeepSeek 服务暂时不可用（HTTP {status}），已自动重试，请稍后再说一次。"
    return f"DeepSeek 请求失败（HTTP {status}），请检查接口地址、模型名及服务状态。"
