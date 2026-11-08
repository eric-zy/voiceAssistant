"""Pronunciation scoring for English conversation, by a cloud speech-assessment service.

Free conversation has no reference sentence, and no model on this machine can tell standard pronunciation
from merely recognisable speech, so this asks a service built for it. Which service, and its key, are the
user's to set (VOICE_PRON_PROVIDER and that provider's variables); until then assess() returns None and the
page says pronunciation is not scored, while content and fluency are still judged.

A provider is a function (pcm16k_bytes, transcript) -> {"overall", "accuracy", "fluency", "completeness",
"words": [{"word", "score"}]} with scores 0-100, or raises ProviderError with a message for the page.
"""
import os

PROVIDERS = {}
WEAK_WORD_SCORE = 60


class ProviderError(Exception):
    pass


def provider_name():
    name = os.getenv("VOICE_PRON_PROVIDER", "").strip().lower()
    return name if name in PROVIDERS else ""


def status():
    """What the page shows about pronunciation scoring."""
    name = os.getenv("VOICE_PRON_PROVIDER", "").strip().lower()
    if not name:
        return "未配置（只评内容和流利度）"
    return f"{name}" if name in PROVIDERS else f"{name} 不支持（只评内容和流利度）"


def assess(pcm, transcript):
    """Scores for one recording, or None when no provider is set up. Errors become None with a reason."""
    name = provider_name()
    if not name or not pcm or not transcript.strip():
        return None
    try:
        result = PROVIDERS[name](pcm, transcript)
    except ProviderError as error:
        return {"error": str(error)}
    except Exception:
        return {"error": "发音评测服务出错"}
    words = [{"word": str(item.get("word", ""))[:40], "score": round(float(item.get("score", 0)))} for item in result.get("words") or []]
    return {key: round(float(result.get(key, 0))) for key in ("overall", "accuracy", "fluency", "completeness")} | {"words": words}


def weak_words(scores):
    """Words worth practising: scored below WEAK_WORD_SCORE, each once."""
    seen, weak = set(), []
    for item in (scores or {}).get("words") or []:
        word = item["word"].lower().strip(".,!?")
        if word and item["score"] < WEAK_WORD_SCORE and word not in seen:
            seen.add(word)
            weak.append(item)
    return weak
