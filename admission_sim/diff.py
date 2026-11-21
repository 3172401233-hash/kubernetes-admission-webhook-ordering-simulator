"""Recursive object diff used to report per-invocation changes."""

_MISSING = object()


def diff_objects(old, new, path=""):
    """Return a list of change records between two JSON-like objects.

    Each record: {"op": "add"|"remove"|"replace", "path": str,
                  "old": value|None, "new": value|None}
    """
    changes = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            child_path = "%s/%s" % (path, _escape(key))
            before = old.get(key, _MISSING)
            after = new.get(key, _MISSING)
            if before is _MISSING:
                changes.append({"op": "add", "path": child_path,
                                "old": None, "new": after})
            elif after is _MISSING:
                changes.append({"op": "remove", "path": child_path,
                                "old": before, "new": None})
            else:
                changes.extend(diff_objects(before, after, child_path))
    elif isinstance(old, list) and isinstance(new, list):
        common = min(len(old), len(new))
        for i in range(common):
            changes.extend(diff_objects(old[i], new[i], "%s/%d" % (path, i)))
        for i in range(common, len(new)):
            changes.append({"op": "add", "path": "%s/%d" % (path, i),
                            "old": None, "new": new[i]})
        for i in range(common, len(old)):
            changes.append({"op": "remove", "path": "%s/%d" % (path, i),
                            "old": old[i], "new": None})
    elif old != new:
        changes.append({"op": "replace", "path": path or "/",
                        "old": old, "new": new})
    return changes


def _escape(token):
    return str(token).replace("~", "~0").replace("/", "~1")
