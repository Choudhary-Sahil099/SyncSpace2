import pytest
from app.ot import Edit, apply_actions, make_edit, transform_edit

def test_non_conflicting_concurrent_insertions():
    earlier = [{"type":"insert","pos":0,"text":"X"}]
    later = transform_edit(Edit(5,0,"Y"), [earlier])
    assert apply_actions("XHello",later) == "XHelloY"

def test_simultaneous_insertions_ordered():
    a = [{"type":"insert","pos":1,"text":"A"}]
    b = transform_edit(Edit(1,0,"B"), [a])
    assert apply_actions("aAb", b) == "aABb"

def test_delete_keeps_concurrent_insertion_inside_range():
    history = [[{"type":"insert","pos":2,"text":"X"}]]
    actions = transform_edit(Edit(1,3,""), history)
    assert apply_actions("abXcde", actions) == "aXe"

def test_overlapping_deletions():
    previous = [{"type":"delete","pos":2,"length":2}]
    result = transform_edit(Edit(1,3,""), [previous])
    assert apply_actions("abef",result) == "aef"

def test_replacement_after_insert():
    previous = [{"type":"insert","pos":0,"text":"!"}]
    actions = transform_edit(Edit(1,2,"xy"), [previous])
    assert apply_actions("!abcd",actions) == "!axyd"

def test_diff():
    assert make_edit("hello", "help") == Edit(3,2,"p")
