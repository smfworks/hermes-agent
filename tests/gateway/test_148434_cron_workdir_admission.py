"""A cron job whose workdir folder was deleted still admits (#148434).

``_create`` used to copy the stored workdir as-is into ``launch['cwd']``; gateway admission
(``build_policy``) then refused the missing directory with a bare ``RuntimeStoreError:
invalid_params`` before the scheduler's ``_resolve_job_workdir`` could drop it. The job failed
on every run with no field or detail. Now admission resolves the workdir the same way the
scheduler does: missing folder → run without it (logged), not dead.

``create_job`` validates the folder at create time, so the deleted-workdir tests mutate the
stored record directly — the same shape a host restore or manual edit leaves behind.
"""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import gateway.session_cron as session_cron
from cron import jobs


@contextmanager
def _job_store(tmp_path):
    """Both the writer and the admission path must see the same store."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        yield


def _authority(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    db = SessionDB(tmp_path / 'state.db')
    runner = SimpleNamespace(_draining=False, session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()),
                             adapters={})
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    return authority


def _stored_workdir(job_id: str, workdir) -> None:
    """Point an existing job's stored workdir at *workdir*, past create-time validation."""

    def apply(stored, _i, record):
        record['workdir'] = workdir
        stored[_i] = record
        jobs.save_jobs(stored)

    jobs._with_job(job_id, apply)


async def test_deleted_workdir_still_admits(tmp_path, monkeypatch):
    authority = _authority(tmp_path, monkeypatch)
    gone = tmp_path / 'deleted-workdir'
    with _job_store(tmp_path):
        seed = tmp_path / 'seed-workdir'
        seed.mkdir()
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local', model='fixture',
                              workdir=str(seed))
        _stored_workdir(job['id'], str(gone))
    assert not gone.exists()

    with _job_store(tmp_path):
        result = await session_cron.operation(
            authority, 'submit', {'job_id': job['id'], 'request_id': 'fire-1', 'extra_prompt': None})
    # Admitted: a session id and an admission row exist — the fire is not dead on arrival.
    assert isinstance(result, dict) and result['session_id']
    assert result['admission_id']


async def test_deleted_workdir_launch_skips_cwd(tmp_path, monkeypatch):
    """_create itself: the launch dict for a deleted workdir carries no cwd (policy default)."""
    import dataclasses

    from gateway import session_policy

    authority = _authority(tmp_path, monkeypatch)
    gone = tmp_path / 'deleted-workdir'
    captured = {}
    real_build_policy = session_policy.build_policy

    def spy_build_policy(params, *a, **k):
        captured['cwd'] = params.get('cwd')
        return real_build_policy(params, *a, **k)

    monkeypatch.setattr(session_policy, 'build_policy', spy_build_policy)
    import gateway.session_cron as sc
    # _create imports build_policy inside the function from gateway.session_policy — patch there.
    with _job_store(tmp_path):
        seed = tmp_path / 'seed-workdir'
        seed.mkdir()
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local', model='fixture',
                              workdir=str(seed))
        _stored_workdir(job['id'], str(gone))

        result = await session_cron.operation(
            authority, 'submit', {'job_id': job['id'], 'request_id': 'fire-1', 'extra_prompt': None})
    assert result['session_id']
    assert captured['cwd'] is None  # dropped, never handed to build_policy


async def test_live_workdir_launch_keeps_cwd(tmp_path, monkeypatch):
    from gateway import session_policy

    authority = _authority(tmp_path, monkeypatch)
    live = tmp_path / 'live-workdir'
    live.mkdir()
    captured = {}
    real_build_policy = session_policy.build_policy

    def spy_build_policy(params, *a, **k):
        captured['cwd'] = params.get('cwd')
        return real_build_policy(params, *a, **k)

    with _job_store(tmp_path):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local', model='fixture',
                              workdir=str(live))
        result = await session_cron.operation(
            authority, 'submit', {'job_id': job['id'], 'request_id': 'fire-1', 'extra_prompt': None})
    assert result['session_id']
    # The live workdir rides through to the frozen policy (cwd defaults to the launch cwd).
    from gateway.session_local_recovery import local_adapter_map
    from gateway.config import Platform
    policy = local_adapter_map(authority)[Platform.LOCAL].policies[result['session_id']]
    request = json.loads(policy.request_json)
    stored_job = request['cron_job']
    # The job record still names the (existing) folder; _create resolved it as-is.
    assert stored_job['workdir'] == str(live)
