import json

from journalflow.intake import apply_fetch_scope


def test_metadata_update_does_not_download_old_unknown_article(config, state, paper):
    config.path.write_text('[automation]\npublication_since="2021-10-30"\n', encoding='utf-8')
    item = dict(paper)
    item['dates'] = {'published': {'date-parts': [[2002, 8]]}}
    state.connection.execute('UPDATE papers SET metadata=? WHERE doi=?', (json.dumps(item), paper['doi']))
    state.connection.commit()
    assert apply_fetch_scope(config, state) == 1
    assert state.pending_fetch(10) == []
    assert state.paper(paper['doi'])['fetch_state'] == 'outside_scope'
    assert apply_fetch_scope(config, state) == 0


def test_scope_preserves_existing_download_and_restores_changed_publication(config, state, paper):
    config.path.write_text('[automation]\npublication_since="2021-10-30"\n', encoding='utf-8')
    item = dict(paper)
    item['dates'] = {'published': {'date-parts': [[2002]]}}
    state.connection.execute('UPDATE papers SET metadata=?,fetch_state=? WHERE doi=?',
                             (json.dumps(item), 'downloaded', paper['doi']))
    state.connection.commit()
    assert apply_fetch_scope(config, state) == 0
    assert state.paper(paper['doi'])['fetch_state'] == 'downloaded'
    state.fetch_result(paper['doi'], 'outside_scope')
    state.connection.execute('UPDATE papers SET metadata=? WHERE doi=?',
                             (json.dumps(paper), paper['doi']))
    state.connection.commit()
    apply_fetch_scope(config, state)
    assert state.paper(paper['doi'])['fetch_state'] == 'pending'


def test_metadata_only_changes_keep_downloaded_and_baseline_states(state, paper):
    for status in ('downloaded', 'baseline_available'):
        state.fetch_result(paper['doi'], status)
        updated = dict(paper, metadata_hash=status, title=status)
        state.upsert(updated)
        assert state.paper(paper['doi'])['fetch_state'] == status
