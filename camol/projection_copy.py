"""Detached copies for JSON-shaped projections without copying atomic scalars.

This is not a cache and does not omit validation. Exact built-in dict/list graphs
are copied with one memo, preserving aliases and cycles. Built-in JSON scalars
are immutable and can be shared safely. If another type appears anywhere, the
whole original graph is instead delegated to ``deepcopy`` so custom hooks and
subclasses retain the existing copy semantics.
"""

from copy import deepcopy


_NONE_TYPE = type(None)


class _NonProjectionValue(Exception):
    pass


def _copy_json_graph(value, memo):
    kind = type(value)
    if kind is str or kind is int or kind is float or kind is bool or kind is _NONE_TYPE:
        return value
    if kind is not dict and kind is not list:
        raise _NonProjectionValue
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if kind is dict:
        result = {}
        memo[identity] = result
        for key, item in value.items():
            key_kind = type(key)
            if (key_kind is not str and key_kind is not int and key_kind is not float
                    and key_kind is not bool and key_kind is not _NONE_TYPE):
                raise _NonProjectionValue
            item_kind = type(item)
            if (item_kind is str or item_kind is int or item_kind is float
                    or item_kind is bool or item_kind is _NONE_TYPE):
                result[key] = item
            else:
                result[key] = _copy_json_graph(item, memo)
    else:
        result = []
        memo[identity] = result
        for item in value:
            item_kind = type(item)
            if (item_kind is str or item_kind is int or item_kind is float
                    or item_kind is bool or item_kind is _NONE_TYPE):
                result.append(item)
            else:
                result.append(_copy_json_graph(item, memo))
    return result


def clone_projection(value, memo=None):
    """Copy projection containers while retaining detached-public-state behavior.

    An explicitly supplied memo may override even scalar identities. Delegate that
    case unchanged; the optimized projection path always owns a fresh private memo.
    Fallback restarts the entire copy, not only a custom subtree: a custom hook can
    deliberately influence another entry through deepcopy's shared memo.
    """
    if memo is not None:
        return deepcopy(value, memo)
    try:
        return _copy_json_graph(value, {})
    except _NonProjectionValue:
        return deepcopy(value)
