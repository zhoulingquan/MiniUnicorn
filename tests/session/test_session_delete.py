"""Tests for SessionManager.delete_session and read_session_file."""

from pathlib import Path

from miniunicorn.session.manager import Session, SessionManager


def _seed(workspace: Path, key: str = "telegram:abc") -> SessionManager:
    sm = SessionManager(workspace)
    session = Session(key=key)
    session.add_message("user", "hello")
    session.add_message("assistant", "hi back")
    sm.save(session)
    return sm


def test_delete_session_removes_file_and_invalidates_cache(tmp_path: Path) -> None:
    sm = _seed(tmp_path, "telegram:abc")
    file_path = sm._get_session_path("telegram:abc")
    assert file_path.exists()
    # Populate cache as a real consumer would.
    cached = sm.get_or_create("telegram:abc")
    assert cached.messages

    assert sm.delete_session("telegram:abc") is True
    assert not file_path.exists()
    # Subsequent get_or_create returns a fresh, empty Session (no stale cache).
    fresh = sm.get_or_create("telegram:abc")
    assert fresh.messages == []


def test_delete_session_returns_false_when_missing(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    assert sm.delete_session("nope:none") is False


def test_read_session_file_returns_metadata_and_messages(tmp_path: Path) -> None:
    sm = _seed(tmp_path, "telegram:abc")
    data = sm.read_session_file("telegram:abc")
    assert data is not None
    assert data["key"] == "telegram:abc"
    assert isinstance(data["messages"], list)
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["created_at"]
    assert data["updated_at"]


def test_read_session_file_does_not_populate_cache(tmp_path: Path) -> None:
    sm = _seed(tmp_path, "telegram:abc")
    sm.invalidate("telegram:abc")
    assert "telegram:abc" not in sm._cache
    sm.read_session_file("telegram:abc")
    assert "telegram:abc" not in sm._cache


def test_read_session_file_missing(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    assert sm.read_session_file("nope:none") is None


def test_safe_key_matches_internal_path(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    key = "telegram:abc/def"
    expected = sm._get_session_path(key).name
    assert SessionManager.safe_key(key) + ".jsonl" == expected


# ---------------------------------------------------------------------------
# v2 filename: sha256-based naming eliminates key collision
# ---------------------------------------------------------------------------


def test_v2_filename_eliminates_key_collision(tmp_path: Path) -> None:
    """``websocket:a:b`` 与 ``websocket:a_b`` 在旧命名下碰撞,v2 必须隔离。"""
    sm = SessionManager(tmp_path)
    key1 = "websocket:a:b"
    key2 = "websocket:a_b"

    path1 = sm._get_session_path(key1)
    path2 = sm._get_session_path(key2)

    # v2 文件名不同(sha256 哈希部分不同)
    assert path1 != path2
    assert path1.name != path2.name

    # 两个 key 各自保存,互不影响
    s1 = Session(key=key1)
    s1.add_message("user", "from key1")
    sm.save(s1)

    s2 = Session(key=key2)
    s2.add_message("user", "from key2")
    sm.save(s2)

    # 各自加载得到自己的消息
    loaded1 = sm.get_or_create(key1)
    loaded2 = sm.get_or_create(key2)
    assert loaded1.messages[0]["content"] == "from key1"
    assert loaded2.messages[0]["content"] == "from key2"


def test_v2_filename_contains_sha256_digest(tmp_path: Path) -> None:
    """v2 文件名应包含 sha256(key) 前 16 位。"""
    import hashlib

    key = "websocket:test:123"
    expected_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    stem = SessionManager.safe_key_v2(key)
    assert expected_digest in stem
    assert "--" in stem


# ---------------------------------------------------------------------------
# Legacy migration: old naming → v2 atomic rename
# ---------------------------------------------------------------------------


def test_legacy_workspace_session_migrates_to_v2(tmp_path: Path) -> None:
    """workspace 内旧命名文件应原子迁移到 v2 路径。"""
    sm = SessionManager(tmp_path)
    key = "telegram:legacy1"

    # 手动写一个旧命名文件
    legacy_path = sm._get_workspace_legacy_session_path(key)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    with open(legacy_path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "_type": "metadata",
                    "key": key,
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "metadata": {},
                    "last_consolidated": 0,
                }
            )
            + "\n"
        )
        f.write(json.dumps({"role": "user", "content": "legacy msg"}) + "\n")

    # get_or_create 触发迁移
    session = sm.get_or_create(key)
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == "legacy msg"

    # 旧文件已迁移(不存在),v2 文件已创建
    assert not legacy_path.exists()
    v2_path = sm._get_session_path(key)
    assert v2_path.exists()


def test_legacy_collision_keeps_old_file_creates_independent_v2(tmp_path: Path) -> None:
    """旧 stem 碰撞时,保留旧文件,两个 key 各自创建独立 v2 文件。"""
    sm = SessionManager(tmp_path)
    import json

    # 两个在旧命名下碰撞的 key
    key1 = "websocket:a:b"
    key2 = "websocket:a_b"
    # 它们的 legacy stem 相同
    assert sm.safe_key_legacy(key1) == sm.safe_key_legacy(key2)

    # 写一个旧命名文件(属于 key1)
    legacy_path = sm._get_workspace_legacy_session_path(key1)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    with open(legacy_path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "_type": "metadata",
                    "key": key1,
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "metadata": {},
                    "last_consolidated": 0,
                }
            )
            + "\n"
        )
        f.write(json.dumps({"role": "user", "content": "belongs to key1"}) + "\n")

    # key1 先迁移,认领 legacy stem
    session1 = sm.get_or_create(key1)
    assert session1.messages[0]["content"] == "belongs to key1"
    assert not legacy_path.exists()  # 已迁移

    # key2 此时无法认领同一 legacy 文件(已被迁移走),创建独立空 v2
    session2 = sm.get_or_create(key2)
    assert session2.messages == []  # 独立空会话,不复制 key1 的数据


# ---------------------------------------------------------------------------
# Tombstone / generation: late save must not resurrect deleted session
# ---------------------------------------------------------------------------


def test_delete_then_late_save_does_not_resurrect(tmp_path: Path) -> None:
    """删除后,持有旧 Session 引用的 late save 不应重新创建文件。"""
    sm = SessionManager(tmp_path)
    key = "telegram:resurrect-test"

    # 创建并保存 session
    session = Session(key=key)
    session.add_message("user", "will be deleted")
    sm.save(session)
    path = sm._get_session_path(key)
    assert path.exists()

    # 模拟:另一处持有旧 Session 引用(如正在执行的 agent task)
    stale_session = sm.get_or_create(key)
    assert stale_session.messages

    # 删除 session
    assert sm.delete_session(key) is True
    assert not path.exists()

    # 旧引用的 late save 不应重新创建文件
    stale_session.add_message("assistant", "late save attempt")
    sm.save(stale_session)
    assert not path.exists(), "late save must not resurrect deleted session file"


def test_recreate_after_delete_uses_new_generation(tmp_path: Path) -> None:
    """删除后重新创建同 key,新 Session 使用新 generation,正常保存。"""
    sm = SessionManager(tmp_path)
    key = "telegram:recreate-test"

    s1 = Session(key=key)
    s1.add_message("user", "first life")
    sm.save(s1)
    path = sm._get_session_path(key)
    assert path.exists()

    sm.delete_session(key)
    assert not path.exists()

    # 重新创建
    s2 = sm.get_or_create(key)
    assert s2.messages == []
    s2.add_message("user", "second life")
    sm.save(s2)
    assert path.exists()

    # 加载得到的是新会话数据
    sm.invalidate(key)
    loaded = sm.get_or_create(key)
    assert loaded.messages[0]["content"] == "second life"
