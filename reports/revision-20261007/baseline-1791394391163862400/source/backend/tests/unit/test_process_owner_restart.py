# Creado por Aldo Garcia.
"""Reinicio seguro: procesos sintéticos, sin señalizar procesos reales."""

from types import SimpleNamespace

import psutil
import pytest

from app.agents import process_owner

pytestmark = pytest.mark.unit


@pytest.fixture
def local_owner(monkeypatch):
    monkeypatch.setattr(process_owner, "local_scope", lambda: "a" * 32)
    monkeypatch.setattr(process_owner.os, "getpid", lambda: 4242)
    monkeypatch.setattr(process_owner.psutil, "Process", lambda _pid: SimpleNamespace(pid=4242, create_time=lambda: 123.5))
    return process_owner.new_owner()


def test_owner_encodes_identity_fits_existing_schema_and_has_unique_nonce(local_owner):
    assert local_owner.startswith("local2:" + "a" * 32 + ":4242:123500000:")
    assert len("released:" + local_owner) <= 128
    assert process_owner.new_owner() != local_owner
    assert process_owner.owner_state(local_owner) == "alive"


def test_dead_process_is_recoverable(local_owner, monkeypatch):
    def missing(_pid):
        raise psutil.NoSuchProcess(4242)

    monkeypatch.setattr(process_owner.psutil, "Process", missing)
    assert process_owner.owner_state(local_owner) == "dead"


def test_reused_pid_does_not_block_restart_or_identify_new_process_as_old(local_owner, monkeypatch):
    monkeypatch.setattr(process_owner.psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: 999.0))
    assert process_owner.owner_state(local_owner) == "dead"


def test_unobservable_process_fails_closed(local_owner, monkeypatch):
    def denied(_pid):
        raise psutil.AccessDenied(4242)

    monkeypatch.setattr(process_owner.psutil, "Process", denied)
    assert process_owner.owner_state(local_owner) == "unknown"


@pytest.mark.parametrize("owner", [
    "5c5f5cfb-3dd2-4a3a-81ca-b6af637f0f5a", "foreign-owner", "",
    "local2:" + "b" * 32 + ":4242:123500000:" + "c" * 32,
    "local2:" + "a" * 32 + ":-1:123500000:" + "c" * 32,
    "local2:" + "a" * 32 + ":4242:NaN:" + "c" * 32,
    "local2:" + "a" * 32 + ":4242:123500000:not-a-nonce",
])
def test_legacy_foreign_and_malformed_owners_never_count_as_local_dead(local_owner, owner):
    assert process_owner.owner_state(owner) == "unknown"
