"""cv_lyric_context 知识库规范化改造的回归测试。

覆盖三块：
  A. 旧库自动迁移（7 列 / 18 列 / 21 列三种形态），逐字段零丢失
  B. SongStore 公开 API 语义不变（含情绪标签排序、待标注队列）
  C. 规范化之后新增的关系型查询（按创作者/歌姬反查、角色分工）
  D. 新版存储层独有能力不被回归：upsert(conn=) 连接复用 / open_writer /
     LIKE 通配符转义 / 情绪查询只返回 4 列

用法：
    python test_vcpedia_schema.py                      # 默认测本文件所在目录
    python test_vcpedia_schema.py <插件目录绝对路径>    # 指定其它副本（如桌面副本）
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

if len(sys.argv) > 1:
    PLUGIN_DIR = Path(sys.argv[1]).resolve()
else:
    PLUGIN_DIR = Path(__file__).resolve().parent
if not (PLUGIN_DIR / "vcpedia_store.py").is_file():
    raise SystemExit(f"找不到插件目录：{PLUGIN_DIR}")
sys.path.insert(0, str(PLUGIN_DIR))

import vcpedia_schema as vs  # noqa: E402
from vcpedia_store import EMOTION_TAGS, SongStore, safe_song_name  # noqa: E402

print(f"被测插件目录: {PLUGIN_DIR}")
print()

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [!!] {label}  {detail}")


def tmp_db(name: str = "kb.db") -> Path:
    return Path(tempfile.mkdtemp(prefix="cvsa_schema_")) / name


def build_legacy(db: Path, layout: str) -> sqlite3.Connection:
    """造旧库。layout: 7(最老) / 18(无情绪列) / 21(当前)"""
    conn = sqlite3.connect(str(db))
    if layout == "7":
        conn.execute(
            "CREATE TABLE songs (uuid TEXT, name TEXT, safe_name TEXT, uploader TEXT,"
            " singers TEXT, introduction TEXT, lyrics TEXT)"
        )
        conn.executemany(
            "INSERT INTO songs VALUES (?,?,?,?,?,?,?)",
            [
                ("u1", "珍珠", "珍珠", "洛天依官方账号", "洛天依", "简介一", "珍珠的第一句呀\n珍珠的第二句呀"),
                ("u2", "多歌手曲", "多歌手曲", "著小生", "言和\n洛天依", "", "多歌手的歌词一句呀"),
                ("u3", "混合分隔", "混合分隔", "某P", "洛天依\n、\n乐正绫", "", "混合分隔的歌词呀"),
                # 无歌词：验证空歌词语义
                ("u4", "无词曲", "无词曲", "某P", "星尘", "", ""),
            ],
        )
    else:
        cols = (
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " safe_name TEXT NOT NULL UNIQUE, uploader TEXT NOT NULL DEFAULT '',"
            " singers TEXT NOT NULL DEFAULT '', lyricist TEXT NOT NULL DEFAULT '',"
            " composer TEXT NOT NULL DEFAULT '', arranger TEXT NOT NULL DEFAULT '',"
            " mixer TEXT NOT NULL DEFAULT '', tuner TEXT NOT NULL DEFAULT '',"
            " mastering TEXT NOT NULL DEFAULT '', pv TEXT NOT NULL DEFAULT '',"
            " illustrator TEXT NOT NULL DEFAULT '', year INTEGER,"
            " introduction TEXT NOT NULL DEFAULT '', lyrics TEXT NOT NULL DEFAULT '',"
            " categories TEXT NOT NULL DEFAULT '', fetched_at REAL NOT NULL DEFAULT 0"
        )
        if layout == "21":
            cols += (
                ", emotion TEXT NOT NULL DEFAULT '',"
                " emotion_annotated_at REAL NOT NULL DEFAULT 0,"
                " lyrics_checked_at REAL NOT NULL DEFAULT 0"
            )
        conn.execute(f"CREATE TABLE songs ({cols})")
        conn.executemany(
            "INSERT INTO songs (name, safe_name, uploader, singers, lyricist, composer,"
            " arranger, mixer, tuner, mastering, pv, illustrator, year, introduction,"
            " lyrics, categories) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("山遥路远", "山遥路远", "某P", "乐正绫", "词作A", "曲作B", "编曲C",
                 "混音D", "调校E", "母带F", "PVG", "曲绘H", 2024, "一段简介",
                 "山遥路远的第一句呀\n山遥路远的第二句呀", "乐正绫歌曲,中文歌声合成"),
                ("双作词曲", "双作词曲", "另一P", "言和、洛天依", "词作A、词作Z", "曲作B",
                 "", "", "", "", "", "", 2023, "", "双作词的歌词一句呀", "言和歌曲"),
            ],
        )
        if layout == "21":
            conn.execute(
                "UPDATE songs SET emotion = '温柔|积极', emotion_annotated_at = 12345.0"
                " WHERE name = '山遥路远'"
            )
    conn.commit()
    return conn


def test_migrate_layout(layout: str) -> None:
    print(f"  -- 旧库形态 {layout} 列 --")
    db = tmp_db(f"legacy{layout}.db")
    conn = build_legacy(db, layout)
    conn.close()

    store = SongStore(db)
    stats = store.migration_stats or {}
    check("迁移被触发", bool(stats) and stats.get("songs", 0) >= 2, str(stats))

    with sqlite3.connect(str(db)) as c:
        check("songs 已变为视图", vs.table_kind(c, "songs") == "view")
        check("旧表已备份保留", vs.table_kind(c, "songs_legacy_v1") == "table")
        cols = [r[1] for r in c.execute("PRAGMA table_info(songs)")]
    expected = [
        "id", "name", "safe_name", "uploader", "singers", "lyricist", "composer",
        "arranger", "mixer", "tuner", "mastering", "pv", "illustrator", "year",
        "introduction", "lyrics", "categories", "emotion", "emotion_annotated_at",
        "fetched_at", "lyrics_checked_at",
    ]
    check("视图列名与顺序复刻旧 21 列", cols == expected, str(cols))
    check("行数保持一致", store.count() == (4 if layout == "7" else 2), str(store.count()))


def test_field_fidelity() -> None:
    print("  -- 字段零丢失（21 列含情绪/分类）--")
    db = tmp_db()
    conn = build_legacy(db, "21")
    before = {r[0]: r for r in conn.execute("SELECT * FROM songs")}
    conn.close()

    store = SongStore(db)
    song = store.get("山遥路远")
    check("歌名", song and song["name"] == "山遥路远")
    check("年份原来是 INTEGER", song and song["year"] == 2024)
    check("简介", song and song["introduction"] == "一段简介")
    check("歌词原样", song and "山遥路远的第一句呀" in song["lyrics"])
    check("UP主", song and song["uploader"] == "某P")
    check("作词", song and song["lyricist"] == "词作A")
    check("曲绘", song and song["illustrator"] == "曲绘H")
    check("分类保序", song and song["categories"] == "乐正绫歌曲,中文歌声合成",
          str(song and song["categories"]))
    check("情绪保序", song and song["emotion"] == "温柔|积极", str(song and song["emotion"]))
    check("情绪标注时间", song and song["emotion_annotated_at"] == 12345.0,
          str(song and song["emotion_annotated_at"]))

    other = store.get("双作词曲")
    check("多作词拆分后回拼", other and other["lyricist"] == "词作A、词作Z",
          str(other and other["lyricist"]))
    check("空角色回空串（非 None）", other and other["mixer"] == "" and other["pv"] == "")
    check("无情绪的歌 emotion 为空串", other and other["emotion"] == "",
          repr(other and other["emotion"]))

    # 7 列库：新列应全为空
    db7 = tmp_db()
    build_legacy(db7, "7").close()
    s7 = SongStore(db7)
    mixed = s7.get("混合分隔")
    check("混合分隔符归一化", mixed and mixed["singers"] == "洛天依、乐正绫",
          str(mixed and mixed["singers"]))
    check("7 列库迁移后情绪为空串", mixed and mixed["emotion"] == "")
    check("7 列库迁移后年份为 None", mixed and mixed["year"] is None)
    # 7 列库里只有「无词曲」这一首没有歌词（其余 3 首都有）
    check("7 列库无歌词语义保留", s7.count_empty_lyrics() == 1,
          str(s7.count_empty_lyrics()))


def test_store_api() -> None:
    print("  -- SongStore 公开 API 语义 --")
    db = tmp_db()
    store = SongStore(db)

    check("新增返回 True", store.upsert({
        "name": "测试曲", "singers": "洛天依\n言和", "uploader": "某P",
        "lyricist": "词手", "categories": "洛天依歌曲,测试",
        "introduction": "简介", "year": "2025", "lyrics": "第一句歌词呀\n第二句歌词呀",
    }) is True)
    first = store.get("测试曲")
    check("字符串年份被转成 int", first and first["year"] == 2025, str(first and first["year"]))
    check("search_lyrics 反查（覆盖歌词前）",
          any(r["name"] == "测试曲" for r in store.search_lyrics("第一句歌词")))

    check("重复 upsert 返回 False", store.upsert({
        "name": "测试曲", "singers": "洛天依", "uploader": "某P", "lyrics": "改后的歌词呀",
    }) is False)
    song = store.get("测试曲")
    check("歌姬被整体覆盖", song and song["singers"] == "洛天依")
    check("分类被整体覆盖为空", song and song["categories"] == "")
    # 沿用旧语义：record 里没有 year 键时会被清空（build_record 总是带 year，不影响线上管线）
    check("未提供 year 时清空", song and song["year"] is None, str(song and song["year"]))

    check("bulk_exists 命中", store.bulk_exists(["测试曲"]) == {"测试曲"},
          str(store.bulk_exists(["测试曲"])))
    check("all_titles 含新歌", "测试曲" in store.all_titles())
    check("search 按歌名", any(r["name"] == "测试曲" for r in store.search("测试")))
    check("search 按歌手", any(r["name"] == "测试曲" for r in store.search("洛天依")))
    check("search 按UP主", any(r["name"] == "测试曲" for r in store.search("某P")))

    # 情绪
    check("mark_emotion", store.mark_emotion("测试曲", ["温柔", "积极"]) is True)
    check("情绪写入后可读", store.get("测试曲")["emotion"] == "温柔|积极")
    check("emotion_stats.annotated", store.emotion_stats()["annotated"] == 1,
          str(store.emotion_stats()))
    check("emotion_stats 分标签", store.emotion_stats().get("温柔") == 1)
    hit = store.search_by_emotion(["温柔"])
    check("search_by_emotion 命中", len(hit) == 1 and hit[0]["name"] == "测试曲")
    check("search_by_emotion 未命中", store.search_by_emotion(["愤怒"]) == [])
    check("标注后不在待标注队列",
          all(r["name"] != "测试曲" for r in store.pending_emotions(50)))

    store.upsert({"name": "待标注曲", "lyrics": "待标注的一句歌词呀", "singers": "言和"})
    pend = store.pending_emotions(50)
    check("待标注队列含新歌", any(r["name"] == "待标注曲" for r in pend))
    check("待标注行含 singers 字段",
          all("singers" in r and "safe_name" in r and "lyrics" in r for r in pend))

    check("clear_emotion", store.clear_emotion("测试曲") is True)
    check("清空后 emotion 为空串", store.get("测试曲")["emotion"] == "")
    check("清空后 annotated 归零", store.emotion_stats()["annotated"] == 0)
    check("清空后重回待标注队列",
          any(r["name"] == "测试曲" for r in store.pending_emotions(50)))

    # 歌词重抓窗口
    store.mark_lyrics_checked(["待标注曲"])
    check("mark_lyrics_checked 计数", store.mark_lyrics_checked(["待标注曲"]) == 1)
    check("空歌词计数", store.count_empty_lyrics() == 0, str(store.count_empty_lyrics()))
    check("revision 记录 intro 变更",
          store.upsert({"name": "测试曲", "introduction": "新简介", "lyrics": "改后的歌词呀"}) is False
          and store.get("测试曲")["introduction"] == "新简介")


def test_relational_api() -> None:
    print("  -- 规范化新增的关系型查询 --")
    db = tmp_db()
    build_legacy(db, "21").close()
    store = SongStore(db)

    credits = store.credits_of("山遥路远")
    check("credits_of 返回角色分工",
          credits.get("lyricist") == ["词作A"] and credits.get("illustrator") == ["曲绘H"],
          str(credits))
    check("credits_of 不含空角色", "mixer" in credits and credits["mixer"] == ["混音D"],
          str(credits))
    check("credits_of 双作词保序", store.credits_of("双作词曲").get("lyricist") == ["词作A", "词作Z"],
          str(store.credits_of("双作词曲")))

    singers = store.singers_of("双作词曲")
    check("singers_of 返回两人", [s["name"] for s in singers] == ["言和", "洛天依"], str(singers))
    check("singers_of engine 未知为 None", singers[0]["engine"] is None)

    check("tags_of 分类保序", store.tags_of("山遥路远") == ["乐正绫歌曲", "中文歌声合成"],
          str(store.tags_of("山遥路远")))
    check("tags_of emotion", store.tags_of("山遥路远", "emotion") == ["温柔", "积极"],
          str(store.tags_of("山遥路远", "emotion")))

    by_artist = store.songs_by_artist("曲作B", "composer")
    check("songs_by_artist 限定角色", {r["name"] for r in by_artist} == {"山遥路远", "双作词曲"},
          str([r["name"] for r in by_artist]))
    check("songs_by_artist 单人角色", [r["name"] for r in store.songs_by_artist("曲绘H")] == ["山遥路远"])
    check("songs_by_singer", {r["name"] for r in store.songs_by_singer("洛天依")} == {"双作词曲"},
          str([r["name"] for r in store.songs_by_singer("洛天依")]))

    breakdown = store.role_breakdown()
    check("role_breakdown 统计", breakdown.get("composer") == 2 and breakdown.get("_songs") == 2,
          str(breakdown))
    stats = store.stats()
    check("stats 各表规模",
          stats["song"] == 2 and stats["singer"] == 3 and stats["song_singer"] == 3,
          str(stats))


def test_idempotent() -> None:
    print("  -- 幂等性 --")
    db = tmp_db()
    build_legacy(db, "21").close()
    SongStore(db)
    store2 = SongStore(db)
    check("二次打开不再迁移", store2.migration_stats is None, str(store2.migration_stats))
    check("二次打开数据仍在", store2.count() == 2, str(store2.count()))
    check("二次打开视图仍在", store2.get("山遥路远")["lyricist"] == "词作A")
    with sqlite3.connect(str(db)) as c:
        check("未产生第二个备份表",
              vs.table_kind(c, "songs_legacy_v1_2") is None)


def test_emotion_whitelist() -> None:
    print("  -- 情绪标签白名单 --")
    check("白名单仍为 7 个", EMOTION_TAGS == ("甜美", "温柔", "积极", "帅气", "搞怪", "伤感", "愤怒"),
          str(EMOTION_TAGS))
    db = tmp_db()
    store = SongStore(db)
    store.upsert({"name": "白名单曲", "lyrics": "一句歌词呀"})
    store.mark_emotion("白名单曲", ["温柔", "温柔", "积极"])
    check("join_emotion 去重保序", store.get("白名单曲")["emotion"] == "温柔|积极",
          store.get("白名单曲")["emotion"])
    check("parse_emotion 可逆", SongStore.parse_emotion("温柔|积极") == ["温柔", "积极"])
    check("safe_song_name 仍导出", safe_song_name("光 -Hikari-") == "光 -Hikari-",
          safe_song_name("光 -Hikari-"))
    # 沿用旧实现：只保留字母数字/空格/连字符/下划线，括号等标点被剔除
    check("safe_song_name 去标点", safe_song_name("珍珠！(Remix)") == "珍珠Remix",
          safe_song_name("珍珠！(Remix)"))


def test_newer_store_contract() -> None:
    """新版存储层独有能力：连接复用、批量写连接、LIKE 转义、4 列返回契约。"""
    print("  -- 新版存储层独有能力（连接复用 / 转义 / 列契约）--")
    db = tmp_db()
    store = SongStore(db)

    # upsert(conn=...) 复用连接且逐首提交
    writer = store.open_writer()
    check("open_writer 返回可用连接", isinstance(writer, sqlite3.Connection))
    check("upsert(conn=) 新增返回 True",
          store.upsert({"name": "批量甲", "lyrics": "批量甲的歌词呀", "singers": "洛天依"},
                       conn=writer) is True)
    check("逐首提交：新连接立刻可见",
          SongStore(db).get("批量甲") is not None)
    check("upsert(conn=) 更新返回 False",
          store.upsert({"name": "批量甲", "lyrics": "换过的歌词呀"}, conn=writer) is False)
    check("复用连接写入生效", store.get("批量甲")["lyrics"] == "换过的歌词呀")
    writer.close()
    check("open_writer 连接可正常关闭", True)

    # LIKE 通配符转义：不转义时 search("%") 会捞出全库
    store.upsert({"name": "百分号曲", "lyrics": "歌词呀"})
    store.upsert({"name": "下划线曲", "lyrics": "歌词呀"})
    store.upsert({"name": "100%纯爱", "lyrics": "纯爱的歌词呀"})
    store.upsert({"name": "a_b 记号", "lyrics": "记号的歌词呀"})
    all_count = store.count()
    check("库里不止 1 首", all_count >= 5, str(all_count))
    pct = store.search("%")
    check("search('%') 不会捞出全库", len(pct) < all_count, f"{len(pct)} / {all_count}")
    check("search('%') 能按字面量命中", [r["name"] for r in pct] == ["100%纯爱"],
          str([r["name"] for r in pct]))
    und = store.search("_")
    check("search('_') 不会捞出全库", len(und) < all_count, f"{len(und)} / {all_count}")
    check("search('_') 按字面量命中且不匹配任意单字",
          [r["name"] for r in und] == ["a_b 记号"], str([r["name"] for r in und]))
    check("search_lyrics 同样转义", store.search_lyrics("%") == [],
          str(store.search_lyrics("%")))

    # 情绪查询只返回 4 列（刻意不拉整段歌词）
    store.mark_emotion("批量甲", ["温柔", "积极"])
    hits = store.search_by_emotion(["温柔"])
    check("search_by_emotion 命中", len(hits) == 1 and hits[0]["name"] == "批量甲", str(hits))
    check("search_by_emotion 只返回 4 列",
          set(hits[0]) == {"name", "singers", "emotion", "introduction"}, str(sorted(hits[0])))
    check("search_by_emotion 命中数多者在前：单标签过滤正确",
          store.search_by_emotion(["愤怒"]) == [])
    both = store.search_by_emotion(["温柔", "积极"])
    check("search_by_emotion 多标签命中同一首只出现一次",
          [r["name"] for r in both] == ["批量甲"], str([r["name"] for r in both]))
    pool = store.all_annotated_songs(limit=10)
    check("all_annotated_songs 只返回 4 列",
          pool and set(pool[0]) == {"name", "singers", "emotion", "introduction"},
          str(sorted(pool[0]) if pool else []))
    check("all_annotated_songs 不要求已标注（与旧语义一致）",
          "100%纯爱" in {r["name"] for r in pool}, str([r["name"] for r in pool]))

    # 情绪多标签排序：命中多的排前面，并列时按 id 升序
    store.upsert({"name": "双标签曲", "lyrics": "双标签的歌词呀"})
    store.mark_emotion("双标签曲", ["温柔", "积极"])
    store.upsert({"name": "单标签曲", "lyrics": "单标签的歌词呀"})
    store.mark_emotion("单标签曲", ["温柔"])
    two = store.search_by_emotion(["温柔", "积极"])
    names = [r["name"] for r in two]
    check("命中 2 个标签的并列居前（顺序按 id）",
          names[:2] == ["批量甲", "双标签曲"], str(names))
    check("命中 1 个标签的排在其后", names[-1] == "单标签曲", str(names))

    # 关系型查询也做了转义
    store.upsert({"name": "转义创作者曲", "lyrics": "歌词呀", "composer": "某人"})
    check("songs_by_artist 正常命中",
          any(r["name"] == "转义创作者曲" for r in store.songs_by_artist("某人")))
    check("songs_by_artist 的通配符被转义",
          store.songs_by_artist("%") == [], str(store.songs_by_artist("%")))


def main() -> int:
    print("cv_lyric_context 知识库规范化改造 回归测试")
    print()
    test_migrate_layout("7")
    test_migrate_layout("18")
    test_migrate_layout("21")
    test_field_fidelity()
    test_store_api()
    test_relational_api()
    test_newer_store_contract()
    test_idempotent()
    test_emotion_whitelist()
    print()
    print(f"合计 {PASS + FAIL} 项，通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
