import itertools

_marker = object()


def pick_all_key(iterable, **kwargs):
    """Return all element having key/value pairs listed in kwargs."""
    def filtermethod(element):
        for k, v in kwargs.iteritems():
            if element[k] != v:
                return False
        return True

    return itertools.ifilter(filtermethod, iterable)


def pick_key(iterable, **kwargs):
    """Return the first element of iterable with all key/value pairs.

    If no matching element is found None is returned.
    """
    try:
        return pick_all_key(iterable, **kwargs).next()
    except StopIteration:
        return None


def pick_all_attr(iterable, **kwargs):
    """Return all element having key/value pairs listed in kwargs."""

    def filtermethod(element):
        for k, v in kwargs.iteritems():
            el = getattr(element, k, _marker)
            if el is _marker or el != v:
                return False
        return True

    return itertools.ifilter(filtermethod, iterable)


def pick_attr(iterable, **kwargs):
    """Return the first element of iterable with all key/value pairs.

    If no matching element is found None is returned.
    """
    try:
        return pick_all_attr(iterable, **kwargs).next()
    except StopIteration:
        return None
