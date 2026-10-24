# -*- coding: utf-8 -*-
"""护住 pan_enroll 的建档口径:严格数页 + list 失败不可当空目录 + 页数门槛。

为什么用 ast 抠源码而不是 import:pan_enroll.py 顶层直接读
`PCID = os.environ["PAN_CID"]`,import 就会 KeyError,而且会真的去连 123。
抠出要测的函数/常量再注入 fake pan,测的仍是**生产源码本身**,不是副本。

三条用例都能打红旧实现:
  1. 旧 count_pages 用 startswith("page_") + 不去重 -> 把 page_0003.jpg 和重复页算进去
  2. 旧实现 list 失败静默返回 0 -> 与「空目录」混同,缺口静默消失
  3. 旧实现无页数门槛 -> 传了 1 页的残件也会入库
"""
import ast
import io
import os
import re

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "pan_enroll.py")


def load_parts(path=SRC):
    """从生产源码里抠出 PAGE_RE / MIN_PAGES / page_nums / should_enroll。"""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"re": re, "os": os}
    got = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("PAGE_RE", "MIN_PAGES"):
                    exec(ast.get_source_segment(src, node), ns)
                    got.add(t.id)
        elif isinstance(node, ast.FunctionDef) and node.name in ("page_nums", "should_enroll", "shortfall_tag"):
            exec(ast.get_source_segment(src, node), ns)
            got.add(node.name)
    missing = {"PAGE_RE", "MIN_PAGES", "page_nums", "should_enroll", "shortfall_tag"} - got
    assert not missing, "源码结构变了,抠不到: %s" % missing
    return ns


def fake_pan(pages):
    """pages: {lastFileId: {"fileList": [...], "lastFileId": next}};缺 key = list 失败"""
    calls = []

    def pan(path):
        calls.append(path)
        last = int(path.rsplit("lastFileId=", 1)[1])
        d = pages.get(last)
        return None if d is None else {"data": d}

    pan.calls = calls
    return pan


def item(name, size, type_=0, trashed=0):
    return {"filename": name, "size": size, "type": type_, "trashed": trashed}


# 一页目录里混进各种不该算的东西
PAGE1 = [
    item("page_0001.webp", 100),
    item("page_0002.webp", 0),          # 0 字节 -> 上传未完成,不算
    item("page_0003.jpg", 500),         # 不是 .webp -> 不算
    item("notes.txt", 10),
    item("page_0100.webp", 200),
    item("page_0001.webp", 100),        # 重复页号 -> 只能算一次
    item("page_0009.webp", 50, type_=1),        # 是目录 -> 不算
    item("page_0008.webp", 70, trashed=1),      # 已删 -> 不算
]
PAGE2 = [item("page_0005.webp", 60)]


def test_strict_naming_size_and_dedup():
    ns = load_parts()
    ns["pan"] = fake_pan({
        0: {"fileList": PAGE1, "lastFileId": 123},
        123: {"fileList": PAGE2, "lastFileId": -1},
    })
    got = ns["page_nums"]("fake-fid")
    assert got == {1, 100, 5}, "严格口径失败: %r" % (got,)
    # 必须真的翻到第二页
    assert len(ns["pan"].calls) == 2


def test_list_failure_is_none_not_empty():
    ns = load_parts()
    ns["pan"] = fake_pan({})                       # 任何一页都拿不到 -> list 失败
    assert ns["page_nums"]("fake-fid") is None, "list 失败被当成空目录,缺口会静默消失"


def test_empty_dir_is_empty_set_not_none():
    ns = load_parts()
    ns["pan"] = fake_pan({0: {"fileList": [], "lastFileId": -1}})
    assert ns["page_nums"]("fake-fid") == set(), "空目录必须是空 set,不能与 list 失败混同"


def test_min_pages_threshold():
    ns = load_parts()
    thr = ns["MIN_PAGES"]
    assert thr == 4, "门槛应与 pan_audit --FIX 一致(4),实际 %r" % (thr,)
    se = ns["should_enroll"]
    assert se(thr - 1) is False, "残件(%d 页)不该入档" % (thr - 1,)
    assert se(thr) is True
    assert se(0) is False
    assert se(None) is False, "list 失败不能入档"


def test_shortfall_tag_splits_empty_and_incomplete():
    """0 页(真空)和 1~3 页(残件)必须分开标,否则「空几个、残几个」查不出来。

    旧实现无条件 return "empty",本条会红:
      2026-10-23 全量建档 679 条全被标成 empty,事后拆分才发现只有 7 条是真真空。
    """
    ns = load_parts()
    tag = ns["shortfall_tag"]
    thr = ns["MIN_PAGES"]

    assert tag(0) == "empty", "0 页必须是 empty"
    for p in range(1, thr):
        assert tag(p) == "incomplete", "%d 页必须标 incomplete,实际 %r" % (p, tag(p))
        assert tag(p) != tag(0), "%d 页与 0 页必须可区分" % (p,)


if __name__ == "__main__":
    # 无 pytest 环境下的自检:python tests/test_pan_enroll_page_nums.py
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  %s" % fn.__name__)
    print("\n%d passed" % len(fns))

