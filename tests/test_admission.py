# -*- coding: utf-8 -*-
"""Tests for the admission webhook chain simulator."""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admission_sim import (AdmissionChain, Rule, ScriptedEndpoint,  # noqa: E402
                           WebhookConfig)
from admission_sim import runtime  # noqa: E402


def pod():
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "demo", "namespace": "default"},
        "spec": {"containers": [{"name": "app", "image": "nginx:1.25"}]},
    }


def pod_rule():
    return Rule(operations=["CREATE"], api_groups=[""], api_versions=["v1"],
                resources=["pods"])


def mutating(name, endpoint, **kw):
    kw.setdefault("rules", [pod_rule()])
    return WebhookConfig(name=name, kind="mutating", endpoint=endpoint, **kw)


def validating(name, endpoint, **kw):
    kw.setdefault("rules", [pod_rule()])
    return WebhookConfig(name=name, kind="validating", endpoint=endpoint, **kw)


class PodInjectionTest(unittest.TestCase):
    """Two mutating webhooks inject a sidecar and an annotation; a validating
    webhook then checks the annotation on the *mutated* object."""

    def setUp(self):
        self.injector = mutating(
            "sidecar-injector",
            ScriptedEndpoint([runtime.ok(patch=[
                {"op": "add", "path": "/spec/containers/-",
                 "value": {"name": "envoy", "image": "envoy:1.30"}},
            ])]),
        )
        self.annotator = mutating(
            "annotation-injector",
            ScriptedEndpoint([runtime.ok(patch=[
                {"op": "add", "path": "/metadata/annotations",
                 "value": {"sidecar-injected": "true"}},
            ])]),
        )
        self.policy = validating(
            "require-sidecar-annotation",
            ScriptedEndpoint([runtime.ok()]),
        )
        self.chain = AdmissionChain([self.injector, self.annotator, self.policy])

    def test_call_order_and_final_object(self):
        result = self.chain.admit(pod())
        self.assertTrue(result.allowed, result.summary())
        names = [r.webhook for r in result.trace]
        self.assertEqual(names, ["sidecar-injector", "annotation-injector",
                                 "require-sidecar-annotation"])
        containers = result.final_object["spec"]["containers"]
        self.assertEqual([c["name"] for c in containers], ["app", "envoy"])
        self.assertEqual(
            result.final_object["metadata"]["annotations"],
            {"sidecar-injected": "true"})

    def test_per_invocation_diff_and_versions(self):
        result = self.chain.admit(pod())
        first, second, third = result.trace
        self.assertEqual(first.version_before, 1)
        self.assertEqual(first.version_after, 2)
        self.assertEqual(second.version_before, 2)
        self.assertEqual(second.version_after, 3)
        # Validating webhooks observe but do not bump the version.
        self.assertEqual(third.version_before, 3)
        self.assertEqual(third.version_after, 3)
        self.assertEqual(result.final_version, 3)
        added_paths = [c["path"] for c in first.diff if c["op"] == "add"]
        self.assertIn("/spec/containers/1", added_paths)

    def test_validating_sees_mutated_object(self):
        seen = {}

        def check(obj, ctx):
            seen["annotations"] = obj["metadata"].get("annotations")
            return runtime.ok()

        chain = AdmissionChain([
            self.injector, self.annotator,
            validating("spy", runtime.FunctionEndpoint(check)),
        ])
        result = chain.admit(pod())
        self.assertTrue(result.allowed)
        self.assertEqual(seen["annotations"], {"sidecar-injected": "true"})


class FieldOverwriteTest(unittest.TestCase):
    """A later mutating webhook overwrites a field set by an earlier one."""

    def test_conflict_recorded_and_last_write_wins(self):
        first = mutating("set-pull-always", ScriptedEndpoint([runtime.ok(patch=[
            {"op": "add", "path": "/spec/containers/0/imagePullPolicy",
             "value": "Always"},
        ])]))
        second = mutating("set-pull-if-not-present",
                          ScriptedEndpoint([runtime.ok(patch=[
                              {"op": "replace",
                               "path": "/spec/containers/0/imagePullPolicy",
                               "value": "IfNotPresent"},
                          ])]))
        chain = AdmissionChain([first, second])
        result = chain.admit(pod())
        self.assertTrue(result.allowed)
        self.assertEqual(
            result.final_object["spec"]["containers"][0]["imagePullPolicy"],
            "IfNotPresent")
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual(conflict["path"], "/spec/containers/0/imagePullPolicy")
        self.assertEqual(conflict["previous"], "set-pull-always")
        self.assertEqual(conflict["current"], "set-pull-if-not-present")


class TimeoutTest(unittest.TestCase):
    def make_chain(self, failure_policy):
        slow_hook = mutating(
            "slow-webhook",
            ScriptedEndpoint([runtime.slow(latency=15.0, patch=[
                {"op": "add", "path": "/metadata/labels",
                 "value": {"slow": "applied"}},
            ])]),
            failure_policy=failure_policy, timeout_seconds=5,
        )
        after = mutating("after-slow", ScriptedEndpoint([runtime.ok(patch=[
            {"op": "add", "path": "/metadata/labels",
             "value": {"after": "applied"}},
        ])]))
        return AdmissionChain([slow_hook, after])

    def test_timeout_with_fail_policy_rejects_and_aborts(self):
        result = self.make_chain("Fail").admit(pod())
        self.assertFalse(result.allowed)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("timeout", result.failures[0])
        rec = result.trace[0]
        self.assertTrue(rec.timed_out)
        # Chain aborted: the second mutating webhook never ran.
        self.assertEqual(len(result.trace), 1)
        self.assertNotIn("labels", result.final_object["metadata"])

    def test_timeout_with_ignore_policy_skips_webhook(self):
        result = self.make_chain("Ignore").admit(pod())
        self.assertTrue(result.allowed, result.summary())
        rec = result.trace[0]
        self.assertTrue(rec.timed_out)
        self.assertTrue(rec.ignored_error)
        # The timed-out webhook's patch was never applied, the next one ran.
        labels = result.final_object["metadata"]["labels"]
        self.assertEqual(labels, {"after": "applied"})
        self.assertEqual(len(result.trace), 2)


class ReinvocationTest(unittest.TestCase):
    """A later webhook mutates the object, so an earlier IfNeeded webhook is
    reinvoked once and its re-run creates a new conflict."""

    def build(self, reinvocation_policy):
        env_hook = mutating(
            "env-defaults",
            ScriptedEndpoint([
                runtime.ok(patch=[{"op": "add",
                                   "path": "/spec/containers/0/env",
                                   "value": [{"name": "MODE", "value": "a"}]}]),
                runtime.ok(patch=[{"op": "replace",
                                   "path": "/spec/containers/0/env/0/value",
                                   "value": "a"}]),
            ]),
            reinvocation_policy=reinvocation_policy,
        )
        overrider = mutating(
            "mode-overrider",
            ScriptedEndpoint([runtime.ok(patch=[
                {"op": "replace", "path": "/spec/containers/0/env/0/value",
                 "value": "b"},
            ])]),
        )
        return AdmissionChain([env_hook, overrider]), env_hook

    def test_if_needed_reinvokes_once_and_reports_reason(self):
        chain, env_hook = self.build("IfNeeded")
        result = chain.admit(pod())
        self.assertTrue(result.allowed, result.summary())
        self.assertEqual(env_hook.endpoint.calls, 2)
        self.assertEqual(len(result.reinvocations), 1)
        self.assertIn("mode-overrider", result.reinvocations[0])
        self.assertIn("IfNeeded", result.reinvocations[0])
        # Round-2 record carries the reason and a bumped version.
        round2 = [r for r in result.trace if r.round == 2]
        self.assertEqual(len(round2), 1)
        self.assertIsNotNone(round2[0].reinvoke_reason)
        # The reinvoked webhook wins the field back: a -> b -> a.
        env = result.final_object["spec"]["containers"][0]["env"]
        self.assertEqual(env, [{"name": "MODE", "value": "a"}])
        # Reinvoke produced a new conflict on the same path.
        conflict_paths = [c["path"] for c in result.conflicts]
        self.assertEqual(conflict_paths.count("/spec/containers/0/env/0/value"),
                         2)
        # Kubernetes reinvokes at most once; the note must say so.
        self.assertTrue(any("at most" in n or "does not guarantee" in n
                            for n in result.notes))

    def test_never_policy_is_not_reinvoked(self):
        chain, env_hook = self.build("Never")
        result = chain.admit(pod())
        self.assertEqual(env_hook.endpoint.calls, 1)
        self.assertEqual(result.reinvocations, [])
        env = result.final_object["spec"]["containers"][0]["env"]
        self.assertEqual(env, [{"name": "MODE", "value": "b"}])


class FailurePolicyTest(unittest.TestCase):
    """Same unreachable webhook, different failurePolicy outcomes."""

    def build(self, failure_policy):
        hook = validating("unreachable-validator",
                          ScriptedEndpoint([runtime.unreachable()]),
                          failure_policy=failure_policy)
        return AdmissionChain([hook])

    def test_fail_policy_rejects(self):
        result = self.build("Fail").admit(pod())
        self.assertFalse(result.allowed)
        self.assertIn("unreachable-validator", result.failures[0])

    def test_ignore_policy_allows(self):
        result = self.build("Ignore").admit(pod())
        self.assertTrue(result.allowed, result.summary())
        self.assertTrue(result.trace[0].ignored_error)


class ValidatingDenialTest(unittest.TestCase):
    def test_denial_basis_is_reported(self):
        guard = validating(
            "image-policy",
            ScriptedEndpoint([runtime.deny("image nginx:1.25 is not allowed")]),
        )
        chain = AdmissionChain([guard])
        result = chain.admit(pod())
        self.assertFalse(result.allowed)
        self.assertEqual(len(result.denials), 1)
        self.assertIn("image-policy", result.denials[0])
        self.assertIn("nginx:1.25", result.denials[0])

    def test_all_validators_run_and_denials_aggregate(self):
        deny_one = validating("deny-one",
                              ScriptedEndpoint([runtime.deny("reason one")]))
        deny_two = validating("deny-two",
                              ScriptedEndpoint([runtime.deny("reason two")]))
        result = AdmissionChain([deny_one, deny_two]).admit(pod())
        self.assertEqual(len(result.denials), 2)


class MatchingTest(unittest.TestCase):
    def test_non_matching_rules_are_skipped(self):
        svc_only = mutating(
            "service-only",
            ScriptedEndpoint([runtime.ok(patch=[
                {"op": "add", "path": "/metadata/labels", "value": {"x": "y"}},
            ])]),
            rules=[Rule(operations=["CREATE"], resources=["services"])],
        )
        result = AdmissionChain([svc_only]).admit(pod())
        self.assertFalse(result.trace[0].matched)
        self.assertNotIn("labels", result.final_object["metadata"])

    def test_namespace_selector(self):
        hook = mutating(
            "team-a-only",
            ScriptedEndpoint([runtime.ok(patch=[
                {"op": "add", "path": "/metadata/labels", "value": {"t": "a"}},
            ])]),
            namespace_selector={"team": "a"},
        )
        chain = AdmissionChain([hook])
        miss = chain.admit(pod(), namespace_labels={"team": "b"})
        self.assertFalse(miss.trace[0].matched)
        hit = chain.admit(pod(), namespace_labels={"team": "a"})
        self.assertTrue(hit.trace[0].matched)
        self.assertEqual(hit.final_object["metadata"]["labels"], {"t": "a"})


class ObjectVersionTest(unittest.TestCase):
    def test_version_only_bumps_on_mutation(self):
        noop = mutating("noop", ScriptedEndpoint([runtime.ok()]))
        changer = mutating("changer", ScriptedEndpoint([runtime.ok(patch=[
            {"op": "add", "path": "/metadata/labels", "value": {"v": "1"}},
        ])]))
        result = AdmissionChain([noop, changer]).admit(pod())
        noop_rec, changer_rec = result.trace
        self.assertEqual(noop_rec.version_before, noop_rec.version_after)
        self.assertEqual(changer_rec.version_after,
                         changer_rec.version_before + 1)
        self.assertEqual(result.final_version, 2)


if __name__ == "__main__":
    unittest.main()
