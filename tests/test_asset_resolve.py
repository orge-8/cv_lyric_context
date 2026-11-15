# -*- coding: utf-8 -*-
"""素材定位与解压缓存（v2.19.0 分发包瘦身）回归测试。

背景：assets 两文件（knowledge_db.db 7.3MB / song_lyric_keywords.txt 5.7MB）
改为 gzip 压缩随包分发（合计 13MB→4.3MB，减重 67%），加载期解压到插件数据
目录的 asset_cache/ 缓存。识别逻辑拿到的仍是普通文件路径，匹配语义零改动。

本文件守住 _resolve_asset 的契约：
1. 未压缩文件优先（兼容旧部署/开发环境，零解压开销）；
2. 仅 .gz 时解压到 data_dir/asset_cache/，内容与源往返一致；
3. 缓存按源 .gz 的 mtime+size 校验：命中不重解压、源变更自动失效重解压；
4. 损坏的 .gz / 缺失文件只返回警告，绝不抛异常（沿用素材缺失降级语义）；
5. 解压是原子替换（.tmp → replace），不留半截缓存。
"""
import gzip
import importlib
import pathlib
import sys

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")

_resolve_asset = _MOD._resolve_asset


@pytest.fixture
def asset_env(tmp_path, monkeypatch):
    """把 ASSET_DIR 指到临时目录，隔离真实素材。返回 (asset_dir, data_dir)。"""
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_MOD, "ASSET_DIR", asset_dir)
    return asset_dir, data_dir


def _write_gz(asset_dir, name, payload: bytes):
    (asset_dir / (name + ".gz")).write_bytes(gzip.compress(payload, compresslevel=9))


def test_raw_file_preferred_over_gz(asset_env):
    """未压缩文件存在时直接用，不解压（即使 .gz 也在）。"""
    asset_dir, data_dir = asset_env
    (asset_dir / "kw.txt").write_text("raw-content", encoding="utf-8")
    _write_gz(asset_dir, "kw.txt", b"gz-content")
    path, warn = _resolve_asset("kw.txt", str(data_dir))
    assert warn is None
    assert path == asset_dir / "kw.txt"
    assert path.read_text(encoding="utf-8") == "raw-content"
    assert not (data_dir / "asset_cache").exists(), "未压缩优先时不应产生解压缓存"


def test_gz_decompresses_to_cache(asset_env):
    """仅 .gz 时解压到 data_dir/asset_cache/，内容往返一致。"""
    asset_dir, data_dir = asset_env
    payload = "歌词句=>《歌名》的歌词\n".encode("utf-8") * 100
    _write_gz(asset_dir, "kw.txt", payload)
    path, warn = _resolve_asset("kw.txt", str(data_dir))
    assert warn is None
    assert path == data_dir / "asset_cache" / "kw.txt"
    assert path.read_bytes() == payload


def test_cache_hit_no_redecompress(asset_env):
    """第二次调用命中缓存：路径不变，且缓存文件 mtime 不被重写。"""
    asset_dir, data_dir = asset_env
    _write_gz(asset_dir, "kw.txt", b"hello")
    p1, _ = _resolve_asset("kw.txt", str(data_dir))
    mtime1 = p1.stat().st_mtime_ns
    p2, warn2 = _resolve_asset("kw.txt", str(data_dir))
    assert warn2 is None
    assert p2 == p1
    assert p2.stat().st_mtime_ns == mtime1, "缓存命中不应重新解压"


def test_cache_invalidated_when_source_changes(asset_env):
    """源 .gz 内容/大小变化 → 缓存失效，重新解压出新内容。"""
    asset_dir, data_dir = asset_env
    _write_gz(asset_dir, "kw.txt", b"old")
    p1, _ = _resolve_asset("kw.txt", str(data_dir))
    assert p1.read_bytes() == b"old"
    # 换一份更大的源（size 变化必然触发失效）
    _write_gz(asset_dir, "kw.txt", b"new-longer-content")
    p2, warn2 = _resolve_asset("kw.txt", str(data_dir))
    assert warn2 is None
    assert p2.read_bytes() == b"new-longer-content"


def test_missing_both_returns_warning(asset_env):
    """未压缩与 .gz 都没有 → (None, 警告)，不抛异常。"""
    _asset_dir, data_dir = asset_env
    path, warn = _resolve_asset("nope.db", str(data_dir))
    assert path is None
    assert warn and "nope.db" in warn


def test_corrupt_gz_degrades(asset_env):
    """损坏的 .gz → (None, 警告)，不抛异常打断索引构建。"""
    asset_dir, data_dir = asset_env
    (asset_dir / "bad.db.gz").write_bytes(b"this is not gzip data")
    path, warn = _resolve_asset("bad.db", str(data_dir))
    assert path is None
    assert warn and "bad.db.gz" in warn


def test_truncated_gz_degrades(asset_env):
    """截断的 .gz（部署拷贝中断）→ 降级警告，不抛异常。"""
    asset_dir, data_dir = asset_env
    full = gzip.compress(b"x" * 10000, compresslevel=9)
    (asset_dir / "trunc.db.gz").write_bytes(full[: len(full) // 2])
    path, warn = _resolve_asset("trunc.db", str(data_dir))
    assert path is None
    assert warn is not None


def test_no_partial_cache_left_on_failure(asset_env):
    """解压失败不应留下可被误用的半截缓存文件。"""
    asset_dir, data_dir = asset_env
    full = gzip.compress(b"y" * 10000, compresslevel=9)
    (asset_dir / "half.db.gz").write_bytes(full[: len(full) // 2])
    _resolve_asset("half.db", str(data_dir))
    cache = data_dir / "asset_cache" / "half.db"
    assert not cache.exists(), "失败时不应留下半截缓存（.tmp 未 replace）"


def test_binary_db_roundtrip(asset_env):
    """二进制 SQLite 库压缩往返字节级一致（素材是二进制，不能按文本处理）。"""
    asset_dir, data_dir = asset_env
    payload = bytes(range(256)) * 50  # 含全部字节值，模拟二进制
    _write_gz(asset_dir, "knowledge_db.db", payload)
    path, warn = _resolve_asset("knowledge_db.db", str(data_dir))
    assert warn is None
    assert path.read_bytes() == payload
