import tomllib

import pytest

from journalflow.codex_setup import connect


def test_connect_uses_local_paths_and_refuses_other_configuration(config, tmp_path):
    script = tmp_path / 'source code' / 'serve-mcp.py'
    script.parent.mkdir()
    script.write_text('# test entry', encoding='utf-8')
    result = connect(config, server_script=script)
    target = config.path.parent / '.codex/config.toml'
    data = tomllib.loads(target.read_text(encoding='utf-8'))
    assert result['global_configuration_modified'] is False
    assert data['mcp_servers']['journalflow']['args'][-1] == config.path.as_posix()
    connect(config, server_script=script)
    target.write_text('[mcp_servers.other]\ncommand="unrelated"\n', encoding='utf-8')
    before = target.read_bytes()
    with pytest.raises(ValueError, match='preserved'):
        connect(config, server_script=script)
    assert target.read_bytes() == before
