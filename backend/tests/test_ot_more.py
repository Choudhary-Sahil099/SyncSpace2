from app.ot import Edit, transform_edit, apply_actions

def test_insertion_inside_prior_deletion():
    actions = transform_edit(Edit(3, 0, 'Z'), [[{"type":"delete","pos":1,"length":3}]])
    assert apply_actions('ae', actions) == 'aZe'

def test_two_history_revisions():
    history = [[{"type":"insert","pos":0,"text":"!"}], [{"type":"insert","pos":2,"text":"X"}]]
    changes = transform_edit(Edit(2,0,'Y'),history)
    assert apply_actions('!aXbc',changes) == '!aXbYc'

def test_edit_replacement_current_revision():
    text = 'hello world'
    changes = transform_edit(Edit(6,5,'team'),[])
    assert apply_actions(text, changes) == 'hello team'

def test_empty_operation():
    assert transform_edit(Edit(0,0,''),[]) == []
