"""Simulated webhook endpoints.

Endpoints are plain in-process callables; no HTTP server and no Kubernetes
API server is involved. Latency is *declared* (not slept), so timeouts are
evaluated deterministically.
"""

from .models import WebhookResponse


class ScriptedEndpoint(object):
    """Returns queued responses in order; repeats the last one when exhausted.

    Useful to model webhooks that behave differently when reinvoked
    (e.g. first call injects a sidecar, reinvocation overwrites a field).
    """

    def __init__(self, responses):
        if not responses:
            raise ValueError("ScriptedEndpoint needs at least one response")
        self._responses = list(responses)
        self.calls = 0

    def call(self, obj, context):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class FunctionEndpoint(object):
    """Wraps a python callable fn(obj, context) -> WebhookResponse."""

    def __init__(self, fn):
        self._fn = fn
        self.calls = 0

    def call(self, obj, context):
        self.calls += 1
        return self._fn(obj, context)


def ok(patch=None, latency=0.0, message=""):
    return WebhookResponse(allowed=True, patch=patch,
                           latency_seconds=latency, status_message=message)


def deny(message, latency=0.0):
    return WebhookResponse(allowed=False, status_message=message,
                           latency_seconds=latency)


def slow(latency, patch=None, message=""):
    """Response whose declared latency exceeds typical timeouts."""
    return WebhookResponse(allowed=True, patch=patch,
                           latency_seconds=latency, status_message=message)


def unreachable(message="connection refused"):
    return WebhookResponse(allowed=False, error=message)
