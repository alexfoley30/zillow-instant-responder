"""The thinnest Firestore query stand-in the cron loops actually use.

`cron._nudge_and_close`, `cron._age_needs_human` and `cron._showing_reminders`
all reach Firestore the same way: `ledger.init_db().collection("zillow_threads")
.where("state", <op>, <value>)[.limit(n)].stream()`. These classes answer
exactly that call shape and nothing more, so the production functions run
unmodified against thread docs keyed by state.

Snapshots carry `.id` and `.to_dict()` because that is all the loops touch
(the same shape tests/test_stuck_alert_once.py already uses for send docs).
"""


class Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Query:
    def __init__(self, snaps):
        self._snaps = list(snaps)

    def limit(self, n):
        return _Query(self._snaps[:n])

    def stream(self):
        return iter(self._snaps)


class _Collection:
    def __init__(self, by_state):
        self._by_state = by_state

    def where(self, field, op, value):
        assert field == "state", f"unexpected filter field {field!r}"
        if op == "==":
            return _Query(self._by_state.get(value, []))
        if op == "in":
            out = []
            for v in value:
                out.extend(self._by_state.get(v, []))
            return _Query(out)
        raise AssertionError(f"unexpected filter op {op!r}")


class FakeDB:
    """by_state maps a ledger state constant -> list of Snap."""

    def __init__(self, by_state):
        self._by_state = {k: list(v) for k, v in by_state.items()}

    def collection(self, name):
        assert name == "zillow_threads", f"unexpected collection {name!r}"
        return _Collection(self._by_state)
