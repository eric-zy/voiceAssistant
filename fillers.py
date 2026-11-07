"""Spoken filler while a tool runs. Only cached audio plays: a clone takes seconds to synthesize."""
import asyncio
import logging

PHRASE = "我查一下。"
log = logging.getLogger("uvicorn.error")


class FillerCache:
    def __init__(self):
        self.audio = {}
        self.warming = {}

    def take(self, voice, synthesize):
        """Return cached audio for this voice, or None and synthesize it for next time."""
        if voice in self.audio:
            return self.audio[voice]
        if voice not in self.warming:
            self.warming[voice] = asyncio.create_task(self._warm(voice, synthesize))
        return None

    async def _warm(self, voice, synthesize):
        try:
            self.audio[voice] = await synthesize(PHRASE)
        except Exception:
            log.warning("filler synthesis failed; tools will run silently")
        finally:
            del self.warming[voice]
