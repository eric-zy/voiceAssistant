"""Primary model first, a backup when it fails before saying anything.

A reply is spoken as it streams, so a provider may be swapped only while nothing has come out yet: a timeout before
the first event, a refused request (429, 5xx, no credit, bad key) or no connection moves on to the next provider and
the user just hears a slightly later answer. Once the first event is out, the reply belongs to that provider; if it
stalls, the user is told, never handed a second answer from another model.

A provider that keeps failing is skipped for a while (circuit breaker), so every turn does not first wait out its
timeout; after the pause one request is let through to see whether it has recovered.
"""
import asyncio
from dataclasses import dataclass, field, replace
import threading
import time

import llm_client


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    extra_body: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    """first_event: seconds to wait for the first event while another provider is left to try (the last one gets
    the client's own timeouts, since there is nothing to switch to). stall: longest gap between events once the
    reply has started. retry_delays: retries within one provider, used only by the last provider."""
    first_event: float
    stall: float
    retry_delays: tuple = llm_client.RETRY_DELAYS


# DeepSeek's first text normally arrives in 0.6 to 0.9 seconds (data/latency.jsonl), so 4 seconds means trouble.
VOICE = Policy(first_event=4, stall=30)
# Background agents are not waited on by a listener; they give the primary longer before moving on.
BACKGROUND = Policy(first_event=30, stall=120)
FAILURES_TO_OPEN, COOLDOWN_SECONDS = 3, 30


class Breaker:
    """Closed until `failures` failures in a row; then open for `cooldown` seconds, after which one request may try
    (half-open). A success closes it again."""

    def __init__(self, failures=FAILURES_TO_OPEN, cooldown=COOLDOWN_SECONDS, clock=time.monotonic):
        self.failures, self.cooldown, self.clock = failures, cooldown, clock
        self.count, self.opened_at, self.lock = 0, None, threading.Lock()

    def available(self):
        with self.lock:
            return self.opened_at is None or self.clock() - self.opened_at >= self.cooldown

    def succeeded(self):
        with self.lock:
            self.count, self.opened_at = 0, None

    def failed(self):
        with self.lock:
            self.count += 1
            if self.count >= self.failures:
                self.opened_at = self.clock()  # a failed half-open try starts a new pause

    def state(self):
        with self.lock:
            if self.opened_at is None:
                return "closed"
            return "half-open" if self.clock() - self.opened_at >= self.cooldown else "open"


async def next_event(stream, seconds=None):
    """The stream's next event, or TimeoutError after `seconds`. asyncio.timeout cancels in this task, so the
    stream (and the HTTP connection inside it) is never resumed from another task, as wait_for would do."""
    if seconds is None:
        return await anext(stream)
    async with asyncio.timeout(seconds):
        return await anext(stream)


class Router:
    """providers() is read on every call, so configuration (and tests) can change it while the server runs."""

    def __init__(self, providers, metrics=None, stream=None, clock=time.monotonic):
        self.providers, self.metrics, self.stream, self.clock = providers, metrics, stream, clock
        self.breakers, self.lock = {}, threading.Lock()

    def breaker(self, name):
        with self.lock:
            return self.breakers.setdefault(name, Breaker(clock=self.clock))

    def configured(self):
        return [provider for provider in self.providers() if provider.api_key and provider.base_url and provider.model]

    def candidates(self, primary_model=None):
        """Providers to try, in order. primary_model replaces the first provider's model (an agent that asks for a
        stronger model); a backup keeps its own, since another host names its models differently."""
        configured = self.configured()
        if primary_model and configured:
            configured[0] = replace(configured[0], model=primary_model)
        healthy = [provider for provider in configured if self.breaker(provider.name).available()]
        return healthy or configured[-1:]  # all paused: still try the last one rather than refuse the user

    def status(self):
        """Each provider's breaker, for the admin's status card."""
        return {provider.name: {"model": provider.model, "state": self.breaker(provider.name).state()} for provider in self.configured()}

    def _count(self, name):
        metrics = self.metrics() if callable(self.metrics) else self.metrics  # looked up per call: tests swap it
        if metrics:
            metrics.count(name)

    async def chat(self, messages, policy=VOICE, on_usage=None, primary_model=None, **options):
        """Yield the same events as llm_client.stream_chat. on_usage(provider, usage) bills the provider that
        actually answered, under the model it used."""
        candidates = self.candidates(primary_model)
        if not candidates:
            raise RuntimeError("没有配置模型服务")
        stream_chat = self.stream or llm_client.stream_chat
        last_error = None
        for position, provider in enumerate(candidates):
            last = position == len(candidates) - 1
            breaker = self.breaker(provider.name)
            stream = stream_chat(provider.base_url, provider.api_key, provider.model, messages,
                                 retry_delays=policy.retry_delays if last else (), extra_body=provider.extra_body,
                                 on_usage=(lambda used, chosen=provider: on_usage(chosen, used)) if on_usage else None, **options)
            try:
                first = await next_event(stream, None if last else policy.first_event)
            except StopAsyncIteration:  # an empty reply is still an answer
                breaker.succeeded()
                return
            except (TimeoutError, llm_client.ProviderUnavailable) as error:
                breaker.failed()
                await stream.aclose()
                last_error = error
                if not last:
                    self._count(f"llm_failover_{provider.name}")
                    continue
                if len(self.configured()) == 1 and isinstance(error, llm_client.ProviderUnavailable):
                    raise  # the only provider configured: its own message says what went wrong
                raise RuntimeError("模型服务暂时不可用，请稍后再试") from error
            except BaseException:
                await stream.aclose()
                raise
            breaker.succeeded()
            if position:
                self._count(f"llm_answered_by_{provider.name}")
            try:
                yield first
                while True:  # from here on the reply stays with this provider
                    try:
                        event = await next_event(stream, policy.stall)
                    except StopAsyncIteration:
                        return
                    except TimeoutError:
                        raise RuntimeError("模型回复中断了，请再说一次") from None
                    yield event
            finally:
                await stream.aclose()
        raise RuntimeError("模型服务暂时不可用，请稍后再试") from last_error
