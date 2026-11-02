"""Identity caches for immutable code objects, without recursively hashing code."""

from collections.abc import MutableMapping, MutableSet


_MISSING = object()


class CodeIdentityMap(MutableMapping):
    """Keep code keys alive while indexing them by their inexpensive identity."""

    def __init__(self):
        self._entries = {}

    def __getitem__(self, code):
        entry = self._entries.get(id(code), _MISSING)
        if entry is _MISSING:
            raise KeyError(code)
        return entry[1]

    def __setitem__(self, code, value):
        self._entries[id(code)] = code, value

    def __delitem__(self, code):
        try:
            del self._entries[id(code)]
        except KeyError:
            raise KeyError(code) from None

    def __contains__(self, code):
        return id(code) in self._entries

    def __iter__(self):
        return (entry[0] for entry in self._entries.values())

    def __len__(self):
        return len(self._entries)

    def get(self, code, default=None):
        entry = self._entries.get(id(code), _MISSING)
        return default if entry is _MISSING else entry[1]

    def pop(self, code, default=_MISSING):
        entry = self._entries.pop(id(code), _MISSING)
        if entry is not _MISSING:
            return entry[1]
        if default is _MISSING:
            raise KeyError(code)
        return default

    def setdefault(self, code, default=None):
        return self._entries.setdefault(id(code), (code, default))[1]

    def clear(self):
        self._entries.clear()


class CodeIdentitySet(MutableSet):
    """A strong identity set, with the same lifetime rule as CodeIdentityMap."""

    def __init__(self):
        self._entries = {}

    def __contains__(self, code):
        return id(code) in self._entries

    def __iter__(self):
        return iter(self._entries.values())

    def __len__(self):
        return len(self._entries)

    def add(self, code):
        self._entries[id(code)] = code

    def discard(self, code):
        self._entries.pop(id(code), None)

    def clear(self):
        self._entries.clear()
