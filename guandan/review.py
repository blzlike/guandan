"""人工校核记录（阶段五，任务书附录 B.3）。

ReviewAnnotation = 人工对某条 AI 决策快照（decision_id 定位）的校核结论，
独立保存于 ``reviews.jsonl``（与快照同目录），**永不回写原始快照与
record**；通过 decision_id 关联 decisions/<seed>.jsonl。

存储为**追加式 JSONL**：每次创建/修改都追加一整行（同 review_id 修改
时 revision +1），``load()`` 按 review_id 折叠保留最新 revision——文件
本身只追加、从不重写，校核历史全程可追溯。

闭环规则（任务书 B.3）：策略调整须先落 regression_case（link_regression）
并测试通过（set_test_status("passed")）后，disposition 才可置 adopted；
违反时 update 抛 ValueError，由测试固化。

引用边界：本模块属复盘/校核侧，仅读写 reviews.jsonl，不依赖 AI 私有
模块；ai/observer 等也不得 import 本模块（校核是人工输入层）。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

DATA_VERSION = 1

CATEGORIES = ("记牌", "推理", "局势", "团队", "候选评价", "炸弹", "拆牌",
              "规则", "信息不足")
VERDICTS = ("correct", "improvable", "error", "insufficient")
VERDICT_LABELS = {"correct": "正确", "improvable": "可改进", "error": "错误",
                  "insufficient": "信息不足"}
TEST_STATUSES = ("pending", "passed", "failed")
DISPOSITIONS = ("open", "adopted", "rejected")
DISPOSITION_LABELS = {"open": "待处理", "adopted": "已采纳", "rejected": "已否决"}

BASE_FIELDS = ("data_version", "review_id", "revision", "decision_id",
               "created_at", "updated_at", "category", "verdict", "note",
               "conditions", "to_regression", "regression_case",
               "test_status", "disposition")

# 可写目录（与 decision/replay_ui 同套 frozen 规则：exe 旁 / 源码目录）
if getattr(sys, "frozen", False):
    _HERE = os.path.dirname(sys.executable)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
REVIEWS_PATH = os.path.join(_HERE, "reviews.jsonl")


def make_review_id(n: int) -> str:
    return f"rv-{n:06d}"


def _now() -> str:
    import datetime
    return f"{datetime.datetime.now():%Y-%m-%d %H:%M}"


def _validate(fields: Dict[str, Any]) -> None:
    """字段合法性校验（创建与修订共用）。"""
    if fields.get("category") not in CATEGORIES:
        raise ValueError(f"非法类别: {fields.get('category')!r}（应为 "
                         f"{'/'.join(CATEGORIES)}）")
    if fields.get("verdict") not in VERDICTS:
        raise ValueError(f"非法结论: {fields.get('verdict')!r}（应为 "
                         f"{'/'.join(VERDICTS)}）")
    if fields.get("test_status") not in TEST_STATUSES:
        raise ValueError(f"非法测试状态: {fields.get('test_status')!r}")
    if fields.get("disposition") not in DISPOSITIONS:
        raise ValueError(f"非法处置状态: {fields.get('disposition')!r}")


class ReviewStore:
    """校核记录 JSONL 存储（追加式）：创建/修订均追加整行，load 折叠。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or REVIEWS_PATH

    # ------------------------------------------------------------- 读取
    def _read_lines(self) -> List[Dict[str, Any]]:
        """逐行解析；损坏行跳过（追加式文件允许中途失败不毁全档）。"""
        if not os.path.exists(self.path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("review_id"):
                    out.append(d)
        return out

    def load(self) -> List[Dict[str, Any]]:
        """全部校核记录（按 review_id 折叠最新 revision，文件序返回）。"""
        latest: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for d in self._read_lines():
            rid = d["review_id"]
            if rid not in latest:
                order.append(rid)
            else:
                old = latest[rid]
                if int(d.get("revision", 1)) < int(old.get("revision", 1)):
                    continue
            latest[rid] = d
        return [latest[rid] for rid in order]

    def find(self, review_id: str) -> Optional[Dict[str, Any]]:
        return next((d for d in self.load() if d["review_id"] == review_id),
                    None)

    def by_decision(self, decision_id: str) -> List[Dict[str, Any]]:
        return [d for d in self.load() if d.get("decision_id") == decision_id]

    def _next_number(self) -> int:
        mx = 0
        for d in self._read_lines():
            rid = d.get("review_id") or ""
            if rid.startswith("rv-") and rid[3:].isdigit():
                mx = max(mx, int(rid[3:]))
        return mx + 1

    # ------------------------------------------------------------- 写入
    def _append(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return dict(rec)

    def append(self, *, decision_id: str, category: str, verdict: str,
               note: str = "", conditions: str = "",
               to_regression: bool = False) -> Dict[str, Any]:
        """新建校核记录（review_id 自增分配，revision=1）。"""
        if not str(decision_id).strip():
            raise ValueError("decision_id 不能为空（须关联决策快照）")
        rec = {
            "data_version": DATA_VERSION,
            "review_id": make_review_id(self._next_number()),
            "revision": 1,
            "decision_id": str(decision_id),
            "created_at": _now(),
            "updated_at": _now(),
            "category": category,
            "verdict": verdict,
            "note": str(note),
            "conditions": str(conditions),
            "to_regression": bool(to_regression),
            "regression_case": None,
            "test_status": "pending",
            "disposition": "open",
        }
        _validate(rec)
        return self._append(rec)

    def update(self, review_id: str, **fields: Any) -> Dict[str, Any]:
        """修订校核记录：按最新版套用变更后**追加**新 revision 行。

        允许修改：category/verdict/note/conditions/to_regression/
        regression_case/test_status/disposition（review_id/created_at/
        decision_id 不可变）。闭环守卫：disposition 置 adopted 须同时
        满足 regression_case 已落且 test_status == passed。
        """
        cur = self.find(review_id)
        if cur is None:
            raise KeyError(f"校核记录不存在: {review_id}")
        banned = {"review_id", "created_at", "decision_id", "revision",
                  "data_version"} & set(fields)
        if banned:
            raise ValueError(f"不可修改字段: {sorted(banned)}")
        rec = dict(cur)
        rec.update(fields)
        if rec.get("to_regression") and not rec.get("regression_case"):
            raise ValueError("纳入回归集须先落 regression_case")
        _validate(rec)
        if rec["disposition"] == "adopted" and not (
                rec.get("regression_case")
                and rec.get("test_status") == "passed"):
            raise ValueError("disposition 置 adopted 前须先落 regression_case "
                             "并测试通过（test_status=passed）")
        rec["revision"] = int(cur.get("revision", 1)) + 1
        rec["updated_at"] = _now()
        return self._append(rec)

    # ------------------------------------------------------- 闭环便捷入口
    def link_regression(self, review_id: str, case_path: str) -> Dict[str, Any]:
        """落回归用例：to_regression=True + regression_case 路径。

        test_status 保持/复位 pending（新用例尚未跑过）。
        """
        return self.update(review_id, to_regression=True,
                           regression_case=str(case_path),
                           test_status="pending")

    def set_test_status(self, review_id: str, status: str) -> Dict[str, Any]:
        """回归执行结果回写（pending/passed/failed）。"""
        return self.update(review_id, test_status=status)

    # ------------------------------------------------------- 导出
    def regression_pending(self) -> List[Dict[str, Any]]:
        """待落回归用例的记录（to_regression 且无 regression_case）。"""
        return [d for d in self.load()
                if d.get("to_regression") and not d.get("regression_case")]
