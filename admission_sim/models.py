"""Data models for the admission webhook simulator.

Python 3.8 compatible.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Failure policies (mirrors admissionregistration.k8s.io/v1)
FAIL = "Fail"
IGNORE = "Ignore"

# Reinvocation policies
NEVER = "Never"
IF_NEEDED = "IfNeeded"

# Webhook kinds
MUTATING = "mutating"
VALIDATING = "validating"


@dataclass
class Rule:
    """A simplified admissionregistration Rule.

    Matches on (operation, api_group, api_version, resource).
    "*" acts as a wildcard for every attribute.
    """

    operations: List[str] = field(default_factory=lambda: ["CREATE"])
    api_groups: List[str] = field(default_factory=lambda: [""])
    api_versions: List[str] = field(default_factory=lambda: ["v1"])
    resources: List[str] = field(default_factory=lambda: ["pods"])
    scope: str = "*"  # "Cluster", "Namespaced" or "*"

    def matches(self, operation, api_group, api_version, resource, namespaced=True):
        if self.scope == "Namespaced" and not namespaced:
            return False
        if self.scope == "Cluster" and namespaced:
            return False
        return (
            _match_any(self.operations, operation)
            and _match_any(self.api_groups, api_group)
            and _match_any(self.api_versions, api_version)
            and _match_any(self.resources, resource)
        )


def _match_any(patterns, value):
    return "*" in patterns or value in patterns


@dataclass
class WebhookConfig:
    """Configuration of one webhook plus its simulated endpoint."""

    name: str
    kind: str  # MUTATING or VALIDATING
    rules: List[Rule] = field(default_factory=list)
    failure_policy: str = FAIL
    reinvocation_policy: str = NEVER  # only meaningful for mutating webhooks
    timeout_seconds: int = 10
    namespace_selector: Optional[Dict[str, str]] = None  # simple matchLabels
    endpoint: Any = None  # object with .call(obj, context) -> WebhookResponse

    def matches(self, operation, api_group, api_version, resource, namespaced=True,
                namespace_labels=None):
        if self.namespace_selector:
            labels = namespace_labels or {}
            for key, val in self.namespace_selector.items():
                if labels.get(key) != val:
                    return False
        return any(
            r.matches(operation, api_group, api_version, resource, namespaced)
            for r in self.rules
        )


@dataclass
class WebhookResponse:
    """What a simulated webhook endpoint returns for one call."""

    allowed: bool = True
    patch: Optional[List[Dict[str, Any]]] = None  # JSON Patch (RFC 6902 subset)
    status_message: str = ""
    latency_seconds: float = 0.0  # declared latency; compared against timeout
    error: Optional[str] = None  # transport-level failure (e.g. connection refused)
