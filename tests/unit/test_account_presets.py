"""Unit tests for account grouping, enabling/disabling, and preset management."""

from __future__ import annotations

from aistudio_api.infrastructure.account.account_store import (
    AccountPreset,
    AccountStore,
)


def test_account_enabled_and_group_persistence(tmp_path):
    store = AccountStore(accounts_dir=tmp_path / "accounts_group_test")

    acc = store.save_account(
        name="Test Account",
        email="test@gmail.com",
        storage_state={"cookies": []},
        enabled=True,
        group="vip",
        notes="Production VIP account",
    )
    assert acc.enabled is True
    assert acc.group == "vip"
    assert acc.notes == "Production VIP account"

    # Toggle enabled
    toggled = store.toggle_account_enabled(acc.id, enabled=False)
    assert toggled is not None
    assert toggled.enabled is False

    # Check persistence
    reloaded = store.get_account(acc.id)
    assert reloaded is not None
    assert reloaded.enabled is False

    # Toggle group
    grouped = store.update_account_group(acc.id, "production")
    assert grouped is not None
    assert grouped.group == "production"
    assert "production" in store.list_groups()


def test_account_preset_crud_and_apply(tmp_path):
    store = AccountStore(accounts_dir=tmp_path / "accounts_preset_test")

    # Save 3 accounts in different tiers and groups
    acc1 = store.save_account(
        name="Acc 1",
        email="acc1@gmail.com",
        storage_state={"cookies": []},
        tier="Free",
        group="default",
        enabled=True,
    )
    acc2 = store.save_account(
        name="Acc 2",
        email="acc2@gmail.com",
        storage_state={"cookies": []},
        tier="Pro",
        group="production",
        enabled=True,
    )
    acc3 = store.save_account(
        name="Acc 3",
        email="acc3@gmail.com",
        storage_state={"cookies": []},
        tier="Ultra",
        group="production",
        enabled=True,
    )

    # 1. Test create snapshot preset
    snapshot = store.create_snapshot_preset("My Custom Preset", "Only acc1 and acc2")
    assert snapshot.id.startswith("preset_")
    assert set(snapshot.account_ids) == {acc1.id, acc2.id, acc3.id}

    # 2. Test apply preset filter_tier (Paid only)
    custom_paid = AccountPreset(
        id="preset_custom_paid",
        name="Paid Only Preset",
        action="filter_tier",
        target_tiers=["Pro", "Ultra"],
    )
    store.save_preset(custom_paid)

    _, enabled_ids, disabled_ids = store.apply_preset("preset_custom_paid")
    assert set(enabled_ids) == {acc2.id, acc3.id}
    assert set(disabled_ids) == {acc1.id}

    # Check accounts status
    a1 = store.get_account(acc1.id)
    a2 = store.get_account(acc2.id)
    a3 = store.get_account(acc3.id)
    assert a1 is not None and a1.enabled is False
    assert a2 is not None and a2.enabled is True
    assert a3 is not None and a3.enabled is True

    # Active account should not be the disabled a1, but automatically fallback to a2
    active_now = store.get_active_account()
    assert active_now is not None
    assert active_now.id == acc2.id


def test_reimport_cookies_preserves_custom_metadata(tmp_path):
    """验证重复导入/刷新已有账号凭据时，不会丢失自定义分组、备注以及停用状态。"""
    store = AccountStore(accounts_dir=tmp_path / "accounts_meta_preserve")

    acc = store.save_account(
        name="Original",
        email="dev@gmail.com",
        storage_state={"cookies": []},
        tier="Pro",
        group="production",
        notes="Important prod key",
        enabled=False,
    )
    assert acc.enabled is False
    assert acc.group == "production"

    # 模拟重新导入凭据（未传 notes / group / enabled）
    updated = store.save_account(
        name="Original",
        email="dev@gmail.com",
        storage_state={"cookies": [{"name": "new_cookie"}]},
        tier="Free",  # 假设本次未探测到高等级
    )
    assert updated.id == acc.id
    assert updated.group == "production"
    assert updated.notes == "Important prod key"
    assert updated.enabled is False
    assert updated.tier == "Pro"  # 保留原有 Pro 等级
