from dataclasses import dataclass

MAX_EDIT = 100_000

@dataclass
class Edit:
    pos: int
    delete: int
    insert: str


def validate(edit: Edit, length: int):
    if edit.pos < 0 or edit.delete < 0 or edit.pos + edit.delete > length or len(edit.insert) > MAX_EDIT:
        raise ValueError("Invalid operation range or insertion size")


def apply_actions(text: str, actions: list[dict]) -> str:
    for a in actions:
        p = a["pos"]
        if a["type"] == "insert":
            if not 0 <= p <= len(text):
                raise ValueError("Invalid insertion")
            text = text[:p] + a["text"] + text[p:]
        elif a["type"] == "delete":
            if not 0 <= p <= len(text) or p + a["length"] > len(text):
                raise ValueError("Invalid deletion")
            text = text[:p] + text[p+a["length"]:]
        else:
            raise ValueError("Unknown action type")
    return text


def _map_after_delete(x: int, p: int, n: int) -> int:
    return x if x <= p else (p if x < p+n else x-n)


def transform_edit(edit: Edit, history: list[list[dict]]) -> list[dict]:
    """Transform a base-revision edit against subsequent committed revisions.

    Represent the user's deletion as multiple surviving intervals. Map them
    through history, retaining any text inserted concurrently inside the range.
    Then execute deletion intervals right-to-left before insertion.
    """
    position = edit.pos
    intervals = [(edit.pos, edit.pos + edit.delete)] if edit.delete else []
    for revision_actions in history:
        for action in revision_actions:
            q = action["pos"]
            if action["type"] == "insert":
                n = len(action["text"])
                if q <= position:
                    position += n
                shifted = []
                for a, b in intervals:
                    if q <= a:
                        shifted.append((a+n, b+n))
                    elif a < q < b:
                        shifted.extend(((a, q), (q+n, b+n)))
                    else:
                        shifted.append((a, b))
                intervals = shifted
            else:
                n = action["length"]
                position = _map_after_delete(position, q, n)
                intervals = [(_map_after_delete(a, q, n), _map_after_delete(b, q, n)) for a,b in intervals]
                intervals = [(a,b) for a,b in intervals if a < b]
    # right-to-left deletions preserve unmodified interval coordinates
    actions = [{"type":"delete", "pos":a, "length":b-a} for a,b in reversed(intervals) if a < b]
    # Adjust insertion index for every deletion preceding it.
    if edit.insert:
        insert_pos = position
        for a,b in intervals:
            insert_pos -= max(0, min(insert_pos, b) - a) if a < insert_pos else 0
        actions.append({"type":"insert", "pos":insert_pos, "text":edit.insert})
    return actions


def make_edit(before: str, after: str) -> Edit:
    """Produce a compact single-range replacement (useful for tests)."""
    a = list(before)
    b = list(after)
    prefix = 0
    while prefix < min(len(a), len(b)) and a[prefix] == b[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(a)-prefix, len(b)-prefix) and a[-1-suffix] == b[-1-suffix]:
        suffix += 1
    return Edit(prefix, len(a)-prefix-suffix, "".join(b[prefix:len(b)-suffix]))
