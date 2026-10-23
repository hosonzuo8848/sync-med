# -*- coding: utf-8 -*-
"""pan_register 扫描段守卫:并发 BFS 必须与原来的串行递归 walk 产出逐条一致。

背景(2026-10-23,创始人):123 账户是 **20 线程**配额,上传线长期占几条,
所以读盘改 12 并发。把串行 `walk` 拆成 `process_dir`(纯函数) + `scan_tree`(线程池 BFS)
是一次高风险语义改造 —— 并发下走错一个分支,扫出来的书单会**静默少几千本**,
日志上看不出任何异常,跑完只表现为「这个月又没新增」。

所以这里不「相信改动」:拿同一棵假目录树把两版实现都跑一遍,逐条比
folders / unrecognized / files_seen;再用不同随机时序跑三轮,确认并发不改变结果。

关键设计:`process_dir` / `scan_tree` / `FULL_ID` / `WORK_ID` / `trailing_vol`
**直接从 scripts/pan_register.py 源码里用 ast 抠出来执行**,不是抄一份副本 ——
副本会在真实代码改动后失效,却仍然显示绿灯,那就成了假守卫。

`python -m pytest tests/test_pan_register_scan.py -q`
"""
import ast
import os
import random
import sys
import textwrap
import time
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from book_id import to_book_id  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "pan_register.py")
KNOWN = {"knownid-0001"}


def d(name, fid):
    return {"type": 1, "filename": name, "fileId": fid}


def f(name):
    return {"type": 0, "filename": name, "fileId": 999}


# fid -> children。这棵树专门覆盖 pan_register 五条分支:
#   ① 分类层下钻 → 聚合层(WORK_ID) → 册文件夹(书名+数字 → agg-NN)
#   ② D1 已知全 ID 直接命中(known)
#   ③ FULL_ID 非 WORK 形态直接挂
#   ④ 聚合层内无册号的目录 → unrecognized
#   ⑤ 深链 depth>=6 → unrecognized(不再下钻)
TREE = {
    0: [d("cat1", 1), d("knownid-0001", 6), d("abc12-3456-1", 7), d("杂项", 8), f("loose.txt")],
    1: [d("zi300-0074", 2), f("a.jpg"), f("b.jpg"), d("abc12-3456-2", 10)],
    2: [d("伤寒论 5", 3), d("zi300-0074-06", 4), d("无编号", 5)],
    3: [f("p1.webp")],
    4: [f("p1.webp")],
    5: [f("x.webp")],
    6: [f("p.webp")],
    7: [f("p.webp")],
    8: [d("d1", 9)],
    9: [d("d2", 11)],
    11: [d("d3", 12)],
    12: [d("d4", 13)],
    13: [d("d5", 14)],
    14: [d("deeper", 15)],   # depth=6,子目录必须落 unrecognized
    15: [f("z.webp")],
    10: [f("p.webp")],
}


def iter_children(fid):
    """假 123 翻页。随机延迟是为了逼出并发乱序:顺序变了结果也不能变。"""
    time.sleep(random.random() * 0.003)
    for it in TREE.get(fid, []):
        yield it


def load_real_scan():
    """从源码抠出真实实现,exec 到独立命名空间。抠不到就红,不许静默跳过。"""
    with open(SCRIPT, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main_fn is not None, "pan_register.py 里找不到 main()"

    want_fn = {"process_dir", "scan_tree", "trailing_vol"}
    want_assign = {"FULL_ID", "WORK_ID"}
    chunks = {"fn": [], "assign": []}
    for node in main_fn.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_fn:
            chunks["fn"].append((node.name, ast.get_source_segment(src, node)))
        elif isinstance(node, ast.Assign):
            names = {getattr(t, "id", None) for t in node.targets}
            if names & want_assign:
                chunks["assign"].append(ast.get_source_segment(src, node))

    got_fn = {n for n, _ in chunks["fn"]}
    missing = want_fn - got_fn
    assert not missing, "源码结构变了,测试需同步更新:缺少 %s" % sorted(missing)
    assert len(chunks["assign"]) == 2, "FULL_ID / WORK_ID 正则未按预期定义"

    body = "\n".join(textwrap.dedent(c) for c in
                     [s for _, s in chunks["fn"]] + chunks["assign"])
    ns = {
        "_re": __import__("re"),
        "to_book_id": to_book_id,
        "iter_children": iter_children,
        "known": KNOWN,
        "folders": [],
        "unrecognized": [],
        "SCAN_CONC": 4,
        "ThreadPoolExecutor": ThreadPoolExecutor,
        "wait": wait,
        "FIRST_COMPLETED": FIRST_COMPLETED,
    }
    exec(compile(body, "<pan_register scan extract>", "exec"), ns)
    return ns, src


def old_scan(root):
    """并发化之前的串行递归 walk,逐字复制自 git 8fffffe(改造前的 origin/main)。"""
    folders, unrecognized = [], []
    files_seen = [0]
    ns, _ = _REAL
    FULL_ID, WORK_ID, trailing_vol = ns["FULL_ID"], ns["WORK_ID"], ns["trailing_vol"]

    def walk(fid, depth, trail, agg=None):
        subdirs = []
        for it in iter_children(fid):
            if it.get("type") == 1:
                subdirs.append((it.get("filename"), it.get("fileId")))
            else:
                files_seen[0] += 1
        for name, cid in subdirs:
            rid = to_book_id(name)
            if agg is not None:
                if (KNOWN is not None and rid in KNOWN) or FULL_ID.match(rid):
                    folders.append((rid, cid))
                    continue
                v = trailing_vol(name)
                if v is not None:
                    folders.append(("%s-%02d" % (agg, v), cid))
                else:
                    unrecognized.append(("%s/%s" % (trail, name), cid))
                continue
            if KNOWN is not None and rid in KNOWN:
                folders.append((rid, cid))
            elif WORK_ID.match(rid):
                walk(cid, depth + 1, "%s/%s" % (trail, name), agg=rid)
            elif FULL_ID.match(rid):
                folders.append((rid, cid))
            elif depth < 6:
                walk(cid, depth + 1, "%s/%s" % (trail, name))
            else:
                unrecognized.append(("%s/%s" % (trail, name), cid))

    walk(root, 0, "~")
    return folders, unrecognized, files_seen[0]


_REAL = None


def new_scan(root, conc=12):
    """跑真实的 scan_tree。folders/unrecognized 是它自己在命名空间里的闭包变量。"""
    ns, _ = _REAL
    ns["folders"].clear()
    ns["unrecognized"].clear()
    ns["SCAN_CONC"] = conc
    files = ns["scan_tree"](root)
    return list(ns["folders"]), list(ns["unrecognized"]), files


class ScanParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _REAL
        _REAL = load_real_scan()
        cls.old = old_scan(0)

    def test_parity_three_random_schedules(self):
        old_f, old_u, old_n = self.old
        self.assertGreater(len(old_f), 0, "参照实现没扫出东西,用例本身失效")
        for i in range(3):
            nf, nu, nn = new_scan(0, conc=12)
            self.assertEqual(sorted(nf), sorted(old_f), "第 %d 轮 folders 与串行版不一致" % i)
            self.assertEqual(sorted(nu), sorted(old_u), "第 %d 轮 unrecognized 与串行版不一致" % i)
            self.assertEqual(nn, old_n, "第 %d 轮 loose_files 计数不一致" % i)

    def test_concurrency_does_not_change_result(self):
        one = new_scan(0, conc=1)
        many = new_scan(0, conc=12)
        self.assertEqual(sorted(one[0]), sorted(many[0]), "conc=1 与 conc=12 的书单不同")
        self.assertEqual(one[2], many[2], "conc=1 与 conc=12 的散文件数不同")

    def test_each_branch(self):
        fs = sorted(b for b, _ in self.old[0])
        us = self.old[1]
        self.assertIn("knownid-0001", fs, "D1 已知全 ID 应直接命中")
        self.assertIn("abc12-3456-1", fs, "FULL_ID 非 WORK 形态应直接挂")
        self.assertIn("zi300-0074-05", fs, "聚合层「书名+数字」应转成 agg-05")
        self.assertIn("zi300-0074-06", fs, "聚合层里自带全 ID 的册应原样挂")
        self.assertEqual(self.old[2], 3, "散文件计数应为 3")
        paths = sorted(p for p, _ in us)
        self.assertTrue(any("无编号" in p for p in paths), "聚合层内无册号目录应落 unrecognized")
        self.assertTrue(any(p.count("/") >= 7 for p in paths), "depth>=6 的深目录应落 unrecognized")


if __name__ == "__main__":
    unittest.main(verbosity=2)
