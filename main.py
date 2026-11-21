# -*- coding: utf-8 -*-
"""Demo: run a simulated admission chain and print the full trace.

Purely local simulation -- no Kubernetes API server is contacted.
"""

import json

from admission_sim import AdmissionChain, Rule, ScriptedEndpoint, WebhookConfig
from admission_sim import runtime

POD_RULE = Rule(operations=["CREATE"], resources=["pods"])


def build_chain():
    sidecar = WebhookConfig(
        name="sidecar-injector", kind="mutating", rules=[POD_RULE],
        reinvocation_policy="IfNeeded", timeout_seconds=5,
        endpoint=ScriptedEndpoint([
            runtime.ok(patch=[
                {"op": "add", "path": "/spec/containers/-",
                 "value": {"name": "envoy", "image": "envoy:1.30"}},
                {"op": "add", "path": "/metadata/annotations",
                 "value": {"sidecar-injected": "true"}},
            ]),
            # Behavior when reinvoked: re-assert the injection marker.
            runtime.ok(patch=[
                {"op": "replace",
                 "path": "/metadata/annotations/sidecar-injected",
                 "value": "true"},
            ]),
        ]),
    )
    pull_policy = WebhookConfig(
        name="pull-policy-default", kind="mutating", rules=[POD_RULE],
        timeout_seconds=5,
        endpoint=ScriptedEndpoint([runtime.ok(patch=[
            {"op": "add", "path": "/spec/containers/0/imagePullPolicy",
             "value": "IfNotPresent"},
        ])]),
    )
    flaky = WebhookConfig(
        name="flaky-labeler", kind="mutating", rules=[POD_RULE],
        failure_policy="Ignore", timeout_seconds=3,
        endpoint=ScriptedEndpoint([runtime.slow(latency=9.0)]),
    )
    guard = WebhookConfig(
        name="image-registry-policy", kind="validating", rules=[POD_RULE],
        failure_policy="Fail", timeout_seconds=5,
        endpoint=ScriptedEndpoint([runtime.ok()]),
    )
    return AdmissionChain([sidecar, pull_policy, flaky, guard])


def main():
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "demo", "namespace": "default"},
        "spec": {"containers": [{"name": "app", "image": "nginx:1.25"}]},
    }
    result = build_chain().admit(pod, operation="CREATE", resource="pods")
    print(result.summary())
    print()
    print("final object:")
    print(json.dumps(result.final_object, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
