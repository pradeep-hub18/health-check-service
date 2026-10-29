import importlib
import sys
from unittest.mock import Mock

import pytest


def test_import_run_has_no_database_or_scheduler_side_effects(monkeypatch):
    factory = Mock(side_effect=AssertionError('must not initialize on import'))
    monkeypatch.setattr('app.create_app', factory)
    sys.modules.pop('run', None)
    importlib.import_module('run')
    factory.assert_not_called()


def test_database_connection_is_bounded_without_default_password(monkeypatch):
    from app.database import get_connection
    connect = Mock()
    monkeypatch.setattr('app.database.psycopg.connect', connect)
    monkeypatch.delenv('DB_PASSWORD', raising=False)
    get_connection()
    kwargs = connect.call_args.kwargs
    assert kwargs['password'] is None
    assert kwargs['connect_timeout'] == 3
    assert 'statement_timeout=3000' in kwargs['options']
    assert 'lock_timeout=2000' in kwargs['options']


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', '0', '-1'])
def test_invalid_shutdown_timeout_fails_before_startup(monkeypatch, value):
    import run
    monkeypatch.setenv('SHUTDOWN_GRACE_SECONDS', value)
    factory = Mock()
    monkeypatch.setattr(run, 'create_app', factory)
    with pytest.raises(ValueError, match='finite and positive'):
        run.main()
    factory.assert_not_called()
