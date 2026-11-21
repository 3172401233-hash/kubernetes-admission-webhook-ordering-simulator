"""Minimal RFC 6902 JSON Patch application (add / remove / replace / test)."""

import copy


class PatchError(Exception):
    pass


def _unescape(token):
    return token.replace("~1", "/").replace("~0", "~")


def _parse_path(path):
    if not path.startswith("/"):
        raise PatchError("invalid JSON pointer: %r" % path)
    return [_unescape(t) for t in path.split("/")[1:]]


def _resolve_parent(doc, tokens):
    node = doc
    for tok in tokens[:-1]:
        if isinstance(node, list):
            node = node[_index(tok, node)]
        elif isinstance(node, dict):
            if tok not in node:
                raise PatchError("missing key %r in path" % tok)
            node = node[tok]
        else:
            raise PatchError("cannot descend into scalar")
    return node, tokens[-1]


def _index(tok, lst):
    if tok == "-":
        return len(lst)
    try:
        return int(tok)
    except ValueError:
        raise PatchError("invalid list index %r" % tok)


def apply_patch(doc, operations):
    """Return a new document with the JSON Patch operations applied."""
    result = copy.deepcopy(doc)
    for op in operations:
        _apply_one(result, op)
    return result


def _apply_one(doc, op):
    kind = op.get("op")
    tokens = _parse_path(op["path"])
    parent, last = _resolve_parent(doc, tokens)

    if kind == "test":
        current = _get(parent, last)
        if current != op.get("value"):
            raise PatchError("test failed at %s" % op["path"])
        return

    if kind == "add":
        value = copy.deepcopy(op.get("value"))
        if isinstance(parent, list):
            idx = _index(last, parent)
            if idx > len(parent):
                raise PatchError("list index out of range")
            parent.insert(idx, value)
        elif isinstance(parent, dict):
            parent[last] = value
        else:
            raise PatchError("cannot add into scalar")
    elif kind == "replace":
        value = copy.deepcopy(op.get("value"))
        if isinstance(parent, list):
            idx = _index(last, parent)
            if idx >= len(parent):
                raise PatchError("replace: list index out of range")
            parent[idx] = value
        elif isinstance(parent, dict):
            if last not in parent:
                raise PatchError("replace: missing key %r" % last)
            parent[last] = value
        else:
            raise PatchError("cannot replace into scalar")
    elif kind == "remove":
        if isinstance(parent, list):
            idx = _index(last, parent)
            if idx >= len(parent):
                raise PatchError("remove: list index out of range")
            parent.pop(idx)
        elif isinstance(parent, dict):
            if last not in parent:
                raise PatchError("remove: missing key %r" % last)
            del parent[last]
        else:
            raise PatchError("cannot remove from scalar")
    else:
        raise PatchError("unsupported op %r" % kind)


def _get(parent, last):
    if isinstance(parent, list):
        return parent[_index(last, parent)]
    return parent[last]
