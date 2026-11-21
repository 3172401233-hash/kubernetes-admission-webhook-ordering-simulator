"""Admission chain execution engine.

Simulates how the Kubernetes API server runs mutating webhooks (in order,
with at most one reinvocation pass) followed by validating webhooks.
Everything is computed locally; no API server is contacted.
"""

import copy

from .diff import diff_objects
from .models import FAIL, IF_NEEDED, MUTATING
from .patch import PatchError, apply_patch


class InvocationRecord(object):
    """One webhook call (or a skipped call) in the admission chain."""

    def __init__(self, order, phase, round_no, webhook):
        self.order = order
        self.phase = phase
        self.round = round_no
        self.webhook = webhook
        self.matched = True
        self.skipped_reason = None
        self.latency_seconds = 0.0
        self.timeout_seconds = None
        self.timed_out = False
        self.error = None
        self.failure_policy = None
        self.ignored_error = False
        self.patch = None
        self.patch_error = None
        self.diff = []
        self.version_before = None
        self.version_after = None
        self.allowed = True
        self.status_message = ""
        self.reinvoke_reason = None

    def to_dict(self):
        return {
            "order": self.order,
            "phase": self.phase,
            "round": self.round,
            "webhook": self.webhook,
            "matched": self.matched,
            "skipped_reason": self.skipped_reason,
            "latency_seconds": self.latency_seconds,
            "timeout_seconds": self.timeout_seconds,
            "timed_out": self.timed_out,
            "error": self.error,
            "failure_policy": self.failure_policy,
            "ignored_error": self.ignored_error,
            "patch": self.patch,
            "patch_error": self.patch_error,
            "diff": self.diff,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "allowed": self.allowed,
            "status_message": self.status_message,
            "reinvoke_reason": self.reinvoke_reason,
        }


class AdmissionResult(object):
    def __init__(self):
        self.allowed = True
        self.final_object = None
        self.final_version = None
        self.trace = []            # list[InvocationRecord]
        self.reinvocations = []    # list[str] human readable reasons
        self.conflicts = []        # list[dict] overwritten-field records
        self.denials = []          # list[str] validating rejection basis
        self.failures = []         # list[str] failurePolicy=Fail aborts
        self.notes = []

    @property
    def rejection_reasons(self):
        return self.failures + self.denials

    def summary(self):
        lines = []
        for rec in self.trace:
            head = "#%d [%s r%d] %s" % (rec.order, rec.phase, rec.round, rec.webhook)
            if not rec.matched:
                lines.append("%s SKIPPED (%s)" % (head, rec.skipped_reason))
                continue
            state = "ALLOW" if rec.allowed else "DENY"
            extra = []
            if rec.timed_out:
                extra.append("timeout %ss>%ss" % (rec.latency_seconds,
                                                  rec.timeout_seconds))
            if rec.error:
                extra.append("error=%s" % rec.error)
            if rec.ignored_error:
                extra.append("ignored(failurePolicy=Ignore)")
            if rec.reinvoke_reason:
                extra.append("reinvoke: %s" % rec.reinvoke_reason)
            if rec.version_before is not None:
                extra.append("v%s->v%s" % (rec.version_before, rec.version_after))
            lines.append("%s %s %s" % (head, state, " ".join(extra)))
            for change in rec.diff:
                lines.append("    %s %s: %r -> %r" % (
                    change["op"], change["path"], change["old"], change["new"]))
        for conflict in self.conflicts:
            lines.append("CONFLICT %s: %s overwrote %s (round %s)" % (
                conflict["path"], conflict["current"], conflict["previous"],
                conflict["round"]))
        for reason in self.rejection_reasons:
            lines.append("REJECT: %s" % reason)
        lines.append("FINAL: %s (object version %s)" % (
            "ALLOWED" if self.allowed else "REJECTED", self.final_version))
        return "\n".join(lines)


class AdmissionChain(object):
    """An ordered chain of webhook configurations."""

    def __init__(self, webhooks):
        self.mutating = [w for w in webhooks if w.kind == MUTATING]
        self.validating = [w for w in webhooks if w.kind != MUTATING]

    def admit(self, obj, operation="CREATE", resource="pods", api_group="",
              api_version="v1", namespaced=True, namespace_labels=None):
        result = AdmissionResult()
        current = copy.deepcopy(obj)
        version = 1
        order = [0]
        field_owners = {}  # json path -> webhook name that last wrote it

        ctx = {
            "operation": operation,
            "resource": resource,
            "api_group": api_group,
            "api_version": api_version,
            "namespaced": namespaced,
            "namespace_labels": namespace_labels or {},
        }

        def matching(webhook):
            return webhook.matches(operation, api_group, api_version, resource,
                                   namespaced, namespace_labels)

        # ---- Mutating phase, round 1 ----
        invoked = []  # (webhook, record, mutated_bool)
        for webhook in self.mutating:
            if not matching(webhook):
                rec = self._record(order, MUTATING, 1, webhook, matched=False,
                                   reason="rules/selector not matched")
                result.trace.append(rec)
                continue
            rec, mutated, fatal = self._invoke_mutating(
                webhook, 1, current, version, ctx, result, order, field_owners)
            result.trace.append(rec)
            invoked.append((webhook, rec, mutated))
            if fatal:
                return self._finish(result, current, version)
            if rec.version_after is not None:
                version = rec.version_after

        # ---- Reinvocation pass (at most one, like Kubernetes) ----
        mutating_positions = [i for i, (_, rec, mutated) in enumerate(invoked)
                              if mutated]
        if mutating_positions:
            last_mutation = max(mutating_positions)
            candidates = []
            for i, (webhook, rec, _) in enumerate(invoked):
                if i < last_mutation and webhook.reinvocation_policy == IF_NEEDED:
                    later = sorted({invoked[j][0].name
                                    for j in mutating_positions if j > i})
                    candidates.append((webhook, rec, later))
            if candidates:
                round2_mutated = False
                for webhook, first_rec, later_mutators in candidates:
                    reason = ("object was mutated by later webhook(s) %s after "
                              "this webhook completed; reinvocationPolicy="
                              "IfNeeded" % ", ".join(later_mutators))
                    result.reinvocations.append("%s: %s" % (webhook.name, reason))
                    rec, mutated, fatal = self._invoke_mutating(
                        webhook, 2, current, version, ctx, result, order,
                        field_owners, reinvoke_reason=reason)
                    result.trace.append(rec)
                    if fatal:
                        return self._finish(result, current, version)
                    if rec.version_after is not None:
                        version = rec.version_after
                    round2_mutated = round2_mutated or mutated
                if round2_mutated:
                    result.notes.append(
                        "reinvocation pass mutated the object; Kubernetes does "
                        "not guarantee a second reinvocation, chain stops here")

        # ---- Validating phase ----
        for webhook in self.validating:
            if not matching(webhook):
                rec = self._record(order, "validating", 1, webhook,
                                   matched=False,
                                   reason="rules/selector not matched")
                result.trace.append(rec)
                continue
            rec, fatal = self._invoke_validating(
                webhook, current, version, ctx, result, order)
            result.trace.append(rec)
            if fatal:
                return self._finish(result, current, version)

        return self._finish(result, current, version)

    # ------------------------------------------------------------------

    def _record(self, order, phase, round_no, webhook, matched=True, reason=None):
        order[0] += 1
        rec = InvocationRecord(order[0], phase, round_no, webhook.name)
        rec.matched = matched
        rec.skipped_reason = reason
        rec.failure_policy = webhook.failure_policy
        rec.timeout_seconds = webhook.timeout_seconds
        return rec

    def _invoke_mutating(self, webhook, round_no, current, version, ctx, result,
                         order, field_owners, reinvoke_reason=None):
        rec = self._record(order, MUTATING, round_no, webhook)
        rec.reinvoke_reason = reinvoke_reason
        rec.version_before = version
        rec.version_after = version

        response = webhook.endpoint.call(copy.deepcopy(current), ctx)
        rec.latency_seconds = response.latency_seconds
        rec.allowed = response.allowed
        rec.status_message = response.status_message

        failed = None
        if response.error:
            failed = "webhook error: %s" % response.error
            rec.error = response.error
        elif response.latency_seconds > webhook.timeout_seconds:
            failed = ("webhook timeout: %.1fs exceeded timeoutSeconds=%d"
                      % (response.latency_seconds, webhook.timeout_seconds))
            rec.timed_out = True
        if failed:
            if webhook.failure_policy == FAIL:
                rec.allowed = False
                result.failures.append("%s rejected the request: %s"
                                       % (webhook.name, failed))
                return rec, False, True
            rec.ignored_error = True
            result.notes.append("%s failed but failurePolicy=Ignore: %s"
                                % (webhook.name, failed))
            return rec, False, False

        if not response.allowed:
            # A mutating webhook may explicitly deny the request.
            result.denials.append("%s denied: %s"
                                  % (webhook.name, response.status_message))
            return rec, False, False

        mutated = False
        if response.patch:
            rec.patch = response.patch
            try:
                new_obj = apply_patch(current, response.patch)
            except PatchError as exc:
                rec.patch_error = str(exc)
                if webhook.failure_policy == FAIL:
                    rec.allowed = False
                    result.failures.append("%s produced an invalid patch: %s"
                                           % (webhook.name, exc))
                    return rec, False, True
                rec.ignored_error = True
                return rec, False, False
            changes = diff_objects(current, new_obj)
            rec.diff = changes
            if changes:
                mutated = True
                version += 1
                rec.version_after = version
                # Kubernetes objects are always maps; update in place so the
                # caller's reference sees the mutation.
                current.clear()
                current.update(new_obj)
                self._track_conflicts(response.patch, webhook.name, round_no,
                                      field_owners, result)
        return rec, mutated, False

    def _invoke_validating(self, webhook, current, version, ctx, result, order):
        rec = self._record(order, "validating", 1, webhook)
        rec.version_before = version
        rec.version_after = version

        response = webhook.endpoint.call(copy.deepcopy(current), ctx)
        rec.latency_seconds = response.latency_seconds
        rec.allowed = response.allowed
        rec.status_message = response.status_message

        failed = None
        if response.error:
            failed = "webhook error: %s" % response.error
            rec.error = response.error
        elif response.latency_seconds > webhook.timeout_seconds:
            failed = ("webhook timeout: %.1fs exceeded timeoutSeconds=%d"
                      % (response.latency_seconds, webhook.timeout_seconds))
            rec.timed_out = True
        if failed:
            if webhook.failure_policy == FAIL:
                rec.allowed = False
                result.failures.append("%s rejected the request: %s"
                                       % (webhook.name, failed))
                return rec, True
            rec.ignored_error = True
            result.notes.append("%s failed but failurePolicy=Ignore: %s"
                                % (webhook.name, failed))
            return rec, False

        if not response.allowed:
            result.denials.append("%s denied: %s"
                                  % (webhook.name, response.status_message))
        return rec, False

    def _track_conflicts(self, patch, webhook_name, round_no, field_owners,
                         result):
        for op in patch:
            if op.get("op") not in ("add", "replace"):
                continue
            path = op.get("path", "")
            # A write conflicts when it touches a path another webhook owns,
            # including writes under (or above) an owned path.
            for owned_path, owner in list(field_owners.items()):
                if owner == webhook_name:
                    continue
                if (path == owned_path
                        or path.startswith(owned_path + "/")
                        or owned_path.startswith(path + "/")):
                    result.conflicts.append({
                        "path": path,
                        "owned_path": owned_path,
                        "previous": owner,
                        "current": webhook_name,
                        "round": round_no,
                    })
                    break
            field_owners[path] = webhook_name

    def _finish(self, result, current, version):
        result.final_object = current
        result.final_version = version
        result.allowed = not result.rejection_reasons
        return result
