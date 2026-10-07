"""学分互认 HTTP API。

业务规则：
- 批量互认：仅当每条申请的外课学分足额、成绩达标，且源课与要求在
  本批及现存"已认/待复核"记录中均未重复时，整批确认计入进度；
  否则整批不写。
- 成绩更正使互认不合格：转为"待复核"，停止计分，但两端占用保留。
- 成绩恢复后须显式复核才能重新计分；复核仍不合格维持待复核；
  可撤销以释放占用。
- 整批改派：把多条"已认"互认整体改派到各自的目标要求。按本批
  原占用同时释放后的状态核对规则/成绩/学分与目标要求占用；任一项
  不合格则整批保持原样，全部合格才更新要求与进度并保留原互认编号。
- 培养方案修订：一次修改多项要求所需学分与替代规则最低成绩，全部
  改动共同生效；随后按新标准复查现存已认互认，失去资格的转待复核
  （停止计分但保留两端占用），原待复核记录即使重新达标也须显式复核。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import db

ACTIVE_STATUSES = ("confirmed", "pending")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="学分互认 API", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- schemas


class CourseIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    credits: int = Field(gt=0, description="正整数学分")
    score: float = Field(ge=0, le=100, description="0~100 分成绩")


class CourseCorrection(BaseModel):
    score: Optional[float] = Field(default=None, ge=0, le=100)
    credits: Optional[int] = Field(default=None, gt=0)


class RequirementIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    required_credits: int = Field(gt=0)


class SubstitutionIn(BaseModel):
    course_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    min_score: float = Field(ge=0, le=100)


class RecognitionItem(BaseModel):
    course_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)


class RecognitionBatchIn(BaseModel):
    items: List[RecognitionItem] = Field(min_length=1)


class ReassignItem(BaseModel):
    recognition_id: int = Field(gt=0)
    target_requirement_id: str = Field(min_length=1, max_length=64)


class ReassignBatchIn(BaseModel):
    items: List[ReassignItem] = Field(min_length=1)


class RequirementChange(BaseModel):
    requirement_id: str = Field(min_length=1, max_length=64)
    required_credits: int = Field(gt=0, description="新的正整数所需学分")


class SubstitutionChange(BaseModel):
    course_id: str = Field(min_length=1, max_length=64)
    requirement_id: str = Field(min_length=1, max_length=64)
    min_score: float = Field(ge=0, le=100, description="新的 0~100 最低成绩")


class ProgramRevisionIn(BaseModel):
    requirement_changes: List[RequirementChange] = Field(default_factory=list)
    substitution_changes: List[SubstitutionChange] = Field(default_factory=list)


# ---------------------------------------------------------------- helpers


def _row_to_recognition(row) -> dict:
    return {
        "id": row["id"],
        "course_id": row["course_id"],
        "requirement_id": row["requirement_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _qualifies(conn, course_id: str, requirement_id: str) -> bool:
    """课程当前是否满足对某要求的互认条件（规则存在、学分足额、成绩达标）。"""
    row = conn.execute(
        """
        SELECT c.credits, c.score, r.required_credits, s.min_score
        FROM external_courses c
        JOIN requirements r ON r.id = ?
        JOIN substitutions s ON s.course_id = c.id AND s.requirement_id = r.id
        WHERE c.id = ?
        """,
        (requirement_id, course_id),
    ).fetchone()
    if row is None:
        return False
    return row["credits"] >= row["required_credits"] and row["score"] >= row["min_score"]


# ---------------------------------------------------------------- 基础登记


@app.post("/courses", status_code=201)
def create_course(body: CourseIn):
    """登记外校课程。"""
    with db.write_tx() as conn:
        if conn.execute(
            "SELECT 1 FROM external_courses WHERE id = ?", (body.id,)
        ).fetchone():
            raise HTTPException(409, f"course {body.id!r} already exists")
        conn.execute(
            "INSERT INTO external_courses (id, credits, score) VALUES (?, ?, ?)",
            (body.id, body.credits, body.score),
        )
    return {"id": body.id, "credits": body.credits, "score": body.score}


@app.get("/courses")
def list_courses():
    with db.read_conn() as conn:
        rows = conn.execute(
            "SELECT id, credits, score FROM external_courses ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


@app.put("/courses/{course_id}")
def correct_course(course_id: str, body: CourseCorrection):
    """成绩/学分更正。

    更正后仍占用该课程的"已认"互认若不再合格，原子地转为"待复核"，
    停止计分但保留两端占用。
    """
    if body.score is None and body.credits is None:
        raise HTTPException(422, "nothing to correct: provide score and/or credits")
    with db.write_tx() as conn:
        course = conn.execute(
            "SELECT id, credits, score FROM external_courses WHERE id = ?",
            (course_id,),
        ).fetchone()
        if course is None:
            raise HTTPException(404, f"course {course_id!r} not found")
        new_score = body.score if body.score is not None else course["score"]
        new_credits = body.credits if body.credits is not None else course["credits"]
        conn.execute(
            "UPDATE external_courses SET score = ?, credits = ? WHERE id = ?",
            (new_score, new_credits, course_id),
        )
        moved = []
        recs = conn.execute(
            "SELECT id, requirement_id FROM recognitions "
            "WHERE course_id = ? AND status = 'confirmed'",
            (course_id,),
        ).fetchall()
        for rec in recs:
            if not _qualifies(conn, course_id, rec["requirement_id"]):
                conn.execute(
                    "UPDATE recognitions SET status = 'pending', "
                    "updated_at = datetime('now') WHERE id = ?",
                    (rec["id"],),
                )
                moved.append(rec["id"])
    return {
        "id": course_id,
        "credits": new_credits,
        "score": new_score,
        "moved_to_pending": moved,
    }


@app.post("/requirements", status_code=201)
def create_requirement(body: RequirementIn):
    """登记本校培养要求。"""
    with db.write_tx() as conn:
        if conn.execute(
            "SELECT 1 FROM requirements WHERE id = ?", (body.id,)
        ).fetchone():
            raise HTTPException(409, f"requirement {body.id!r} already exists")
        conn.execute(
            "INSERT INTO requirements (id, required_credits) VALUES (?, ?)",
            (body.id, body.required_credits),
        )
    return {"id": body.id, "required_credits": body.required_credits}


@app.get("/requirements")
def list_requirements():
    with db.read_conn() as conn:
        rows = conn.execute(
            "SELECT id, required_credits FROM requirements ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/substitutions", status_code=201)
def create_substitution(body: SubstitutionIn):
    """登记课程可替代的要求及最低成绩。"""
    with db.write_tx() as conn:
        if not conn.execute(
            "SELECT 1 FROM external_courses WHERE id = ?", (body.course_id,)
        ).fetchone():
            raise HTTPException(404, f"course {body.course_id!r} not found")
        if not conn.execute(
            "SELECT 1 FROM requirements WHERE id = ?", (body.requirement_id,)
        ).fetchone():
            raise HTTPException(404, f"requirement {body.requirement_id!r} not found")
        if conn.execute(
            "SELECT 1 FROM substitutions WHERE course_id = ? AND requirement_id = ?",
            (body.course_id, body.requirement_id),
        ).fetchone():
            raise HTTPException(
                409,
                f"substitution ({body.course_id!r}, {body.requirement_id!r}) "
                "already exists",
            )
        conn.execute(
            "INSERT INTO substitutions (course_id, requirement_id, min_score) "
            "VALUES (?, ?, ?)",
            (body.course_id, body.requirement_id, body.min_score),
        )
    return body.model_dump()


@app.get("/substitutions")
def list_substitutions():
    with db.read_conn() as conn:
        rows = conn.execute(
            "SELECT course_id, requirement_id, min_score "
            "FROM substitutions ORDER BY course_id, requirement_id"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 培养方案修订


@app.post("/program/revisions")
def revise_program(batch: ProgramRevisionIn):
    """培养方案修订：一次修改多项要求所需学分与替代规则最低成绩。

    - 至少提交一项改动；同一要求 / 同一替代规则不得在本批重复出现；
      引用不存在或数值无效（由 schema 拦截）则整次不生效（409 回滚）。
    - 全部改动在一个写事务内共同生效，随后按新标准重新检查现存
      "已认"互认：不再合格的转"待复核"，停止计分但继续占用两端；
      原本"待复核"的记录即使按新标准重新达标也保持待复核，须显式复核。
    - 成功返回已修改项目与转待复核的互认编号；之后的申请、改派与复核
      一律按新标准判断，已认进度按新学分计算。
    """
    if not batch.requirement_changes and not batch.substitution_changes:
        raise HTTPException(422, "nothing to revise: provide at least one change")

    errors: List[str] = []
    seen_requirements, seen_rules = set(), set()
    for i, change in enumerate(batch.requirement_changes):
        if change.requirement_id in seen_requirements:
            errors.append(
                f"requirement_changes[{i}]: requirement "
                f"{change.requirement_id!r} duplicated in batch"
            )
        seen_requirements.add(change.requirement_id)
    for i, change in enumerate(batch.substitution_changes):
        key = (change.course_id, change.requirement_id)
        if key in seen_rules:
            errors.append(
                f"substitution_changes[{i}]: substitution "
                f"({change.course_id!r}, {change.requirement_id!r}) "
                "duplicated in batch"
            )
        seen_rules.add(key)
    if errors:
        raise HTTPException(409, errors)

    with db.write_tx() as conn:
        for change in batch.requirement_changes:
            if conn.execute(
                "SELECT 1 FROM requirements WHERE id = ?",
                (change.requirement_id,),
            ).fetchone() is None:
                errors.append(
                    f"requirement {change.requirement_id!r} not found"
                )
        for change in batch.substitution_changes:
            if conn.execute(
                "SELECT 1 FROM substitutions WHERE course_id = ? AND requirement_id = ?",
                (change.course_id, change.requirement_id),
            ).fetchone() is None:
                errors.append(
                    f"substitution ({change.course_id!r}, "
                    f"{change.requirement_id!r}) not found"
                )
        if errors:
            # 抛出异常 -> write_tx 回滚，整次修订不生效
            raise HTTPException(409, errors)

        # 所有改动共同生效（同一事务内先更新标准，再按新标准复查）
        for change in batch.requirement_changes:
            conn.execute(
                "UPDATE requirements SET required_credits = ? WHERE id = ?",
                (change.required_credits, change.requirement_id),
            )
        for change in batch.substitution_changes:
            conn.execute(
                "UPDATE substitutions SET min_score = ? "
                "WHERE course_id = ? AND requirement_id = ?",
                (change.min_score, change.course_id, change.requirement_id),
            )

        # 仅重新检查现存"已认"互认：失去资格的转待复核；
        # 原待复核记录不动，即使按新标准重新达标也须显式复核。
        moved: List[int] = []
        recs = conn.execute(
            "SELECT id, course_id, requirement_id FROM recognitions "
            "WHERE status = 'confirmed' ORDER BY id"
        ).fetchall()
        for rec in recs:
            if not _qualifies(conn, rec["course_id"], rec["requirement_id"]):
                conn.execute(
                    "UPDATE recognitions SET status = 'pending', "
                    "updated_at = datetime('now') WHERE id = ?",
                    (rec["id"],),
                )
                moved.append(rec["id"])

    return {
        "revised": {
            "requirements": [
                {
                    "requirement_id": c.requirement_id,
                    "required_credits": c.required_credits,
                }
                for c in batch.requirement_changes
            ],
            "substitutions": [
                {
                    "course_id": c.course_id,
                    "requirement_id": c.requirement_id,
                    "min_score": c.min_score,
                }
                for c in batch.substitution_changes
            ],
        },
        "moved_to_pending": moved,
    }


# ---------------------------------------------------------------- 互认


@app.post("/recognitions", status_code=201)
def submit_recognitions(batch: RecognitionBatchIn):
    """批量提交互认：全部合格且无任何重复才整批确认，否则整批不写。"""
    errors: List[str] = []
    seen_courses, seen_requirements = set(), set()
    for i, item in enumerate(batch.items):
        if item.course_id in seen_courses:
            errors.append(f"items[{i}]: course {item.course_id!r} duplicated in batch")
        if item.requirement_id in seen_requirements:
            errors.append(
                f"items[{i}]: requirement {item.requirement_id!r} duplicated in batch"
            )
        seen_courses.add(item.course_id)
        seen_requirements.add(item.requirement_id)
    if errors:
        raise HTTPException(409, errors)

    with db.write_tx() as conn:
        for i, item in enumerate(batch.items):
            course = conn.execute(
                "SELECT credits, score FROM external_courses WHERE id = ?",
                (item.course_id,),
            ).fetchone()
            if course is None:
                errors.append(f"items[{i}]: course {item.course_id!r} not found")
                continue
            req = conn.execute(
                "SELECT required_credits FROM requirements WHERE id = ?",
                (item.requirement_id,),
            ).fetchone()
            if req is None:
                errors.append(
                    f"items[{i}]: requirement {item.requirement_id!r} not found"
                )
                continue
            rule = conn.execute(
                "SELECT min_score FROM substitutions "
                "WHERE course_id = ? AND requirement_id = ?",
                (item.course_id, item.requirement_id),
            ).fetchone()
            if rule is None:
                errors.append(
                    f"items[{i}]: no substitution rule for "
                    f"({item.course_id!r}, {item.requirement_id!r})"
                )
                continue
            if course["credits"] < req["required_credits"]:
                errors.append(
                    f"items[{i}]: course {item.course_id!r} credits "
                    f"{course['credits']} < required {req['required_credits']}"
                )
            if course["score"] < rule["min_score"]:
                errors.append(
                    f"items[{i}]: course {item.course_id!r} score "
                    f"{course['score']} < min_score {rule['min_score']}"
                )
            if conn.execute(
                "SELECT 1 FROM recognitions WHERE course_id = ? "
                "AND status IN ('confirmed', 'pending')",
                (item.course_id,),
            ).fetchone():
                errors.append(
                    f"items[{i}]: course {item.course_id!r} already occupied "
                    "by an active recognition"
                )
            if conn.execute(
                "SELECT 1 FROM recognitions WHERE requirement_id = ? "
                "AND status IN ('confirmed', 'pending')",
                (item.requirement_id,),
            ).fetchone():
                errors.append(
                    f"items[{i}]: requirement {item.requirement_id!r} already "
                    "occupied by an active recognition"
                )
        if errors:
            # 抛出异常 -> write_tx 回滚，整批不写
            raise HTTPException(409, errors)

        created = []
        for item in batch.items:
            cur = conn.execute(
                "INSERT INTO recognitions (course_id, requirement_id, status) "
                "VALUES (?, ?, 'confirmed')",
                (item.course_id, item.requirement_id),
            )
            created.append(
                {
                    "id": cur.lastrowid,
                    "course_id": item.course_id,
                    "requirement_id": item.requirement_id,
                    "status": "confirmed",
                }
            )
    return {"created": created}


@app.get("/recognitions")
def list_recognitions(
    status: Optional[str] = Query(default=None, pattern="^(confirmed|pending)$"),
):
    """按状态查询互认记录（confirmed=已认，pending=待复核）。"""
    sql = (
        "SELECT id, course_id, requirement_id, status, created_at, updated_at "
        "FROM recognitions"
    )
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY id"
    with db.read_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_recognition(r) for r in rows]


@app.post("/recognitions/reassign")
def reassign_recognitions(batch: ReassignBatchIn):
    """整批改派已认互认到新的培养要求。

    - 互认编号、目标要求编号在本批内均不得重复；
    - 来源必须是现存"已认"记录（待复核记录不能改派）；
    - 按本批原占用"同时释放"后的状态核对：每条课程对目标要求的
      替代规则、当前成绩与学分都须合格，目标要求不得被批外的
      "已认/待复核"记录占用（因此两条记录互换要求不会互相冲突）；
    - 任一项不合格则整批不写，原互认与进度保持不变；
    - 全部合格则保留原互认编号，更新要求（及 updated_at），返回
      编号、原要求与新要求。

    与成绩更正、复核、互认申请/撤销共用 write_tx 进程锁，串行执行；
    之后的操作一律按改派后的关系判断。
    """
    errors: List[str] = []
    seen_recognitions, seen_targets = set(), set()
    for i, item in enumerate(batch.items):
        if item.recognition_id in seen_recognitions:
            errors.append(
                f"items[{i}]: recognition {item.recognition_id} duplicated in batch"
            )
        if item.target_requirement_id in seen_targets:
            errors.append(
                f"items[{i}]: target requirement "
                f"{item.target_requirement_id!r} duplicated in batch"
            )
        seen_recognitions.add(item.recognition_id)
        seen_targets.add(item.target_requirement_id)
    if errors:
        raise HTTPException(409, errors)

    batch_ids = [item.recognition_id for item in batch.items]
    placeholders = ",".join("?" for _ in batch_ids)

    with db.write_tx() as conn:
        originals: List[dict] = []
        for i, item in enumerate(batch.items):
            prefix = f"items[{i}]"
            rec = conn.execute(
                "SELECT id, course_id, requirement_id, status "
                "FROM recognitions WHERE id = ?",
                (item.recognition_id,),
            ).fetchone()
            if rec is None:
                errors.append(
                    f"{prefix}: recognition {item.recognition_id} not found"
                )
                continue
            if rec["status"] != "confirmed":
                errors.append(
                    f"{prefix}: recognition {item.recognition_id} is "
                    f"{rec['status']!r}, only 'confirmed' can be reassigned"
                )
                continue

            req = conn.execute(
                "SELECT required_credits FROM requirements WHERE id = ?",
                (item.target_requirement_id,),
            ).fetchone()
            if req is None:
                errors.append(
                    f"{prefix}: requirement {item.target_requirement_id!r} not found"
                )
                continue
            course = conn.execute(
                "SELECT credits, score FROM external_courses WHERE id = ?",
                (rec["course_id"],),
            ).fetchone()
            if course is None:  # 外键约束下理论上不会发生
                errors.append(
                    f"{prefix}: course {rec['course_id']!r} not found"
                )
                continue
            rule = conn.execute(
                "SELECT min_score FROM substitutions "
                "WHERE course_id = ? AND requirement_id = ?",
                (rec["course_id"], item.target_requirement_id),
            ).fetchone()
            if rule is None:
                errors.append(
                    f"{prefix}: no substitution rule for "
                    f"({rec['course_id']!r}, {item.target_requirement_id!r})"
                )
            elif course["score"] < rule["min_score"]:
                errors.append(
                    f"{prefix}: course {rec['course_id']!r} score "
                    f"{course['score']} < min_score {rule['min_score']}"
                )
            if course["credits"] < req["required_credits"]:
                errors.append(
                    f"{prefix}: course {rec['course_id']!r} credits "
                    f"{course['credits']} < required {req['required_credits']}"
                )

            # 目标要求的占用核对按"本批原占用同时释放"后的状态：
            # 占用者属于本批改派来源（将要让出）则不视为冲突，
            # 批外的已认/待复核记录一律拒绝。
            occupant = conn.execute(
                "SELECT id, status FROM recognitions "
                f"WHERE requirement_id = ? AND status IN ('confirmed', 'pending') "
                f"AND id NOT IN ({placeholders})",
                (item.target_requirement_id, *batch_ids),
            ).fetchone()
            if occupant is not None:
                errors.append(
                    f"{prefix}: target requirement "
                    f"{item.target_requirement_id!r} already occupied by "
                    f"recognition {occupant['id']} ({occupant['status']}) "
                    "outside this batch"
                )

            originals.append(
                {
                    "id": item.recognition_id,
                    "from_requirement_id": rec["requirement_id"],
                    "to_requirement_id": item.target_requirement_id,
                }
            )

        if errors:
            # 抛出异常 -> write_tx 回滚，原互认与进度整批保持不变
            raise HTTPException(409, errors)

        reassigned = []
        for target in originals:
            conn.execute(
                "UPDATE recognitions SET requirement_id = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (target["to_requirement_id"], target["id"]),
            )
            reassigned.append(target)
    return {"reassigned": reassigned}


@app.post("/recognitions/{recognition_id}/review")
def review_recognition(recognition_id: int):
    """复核待复核记录：合格则重新计分（confirmed），仍不合格维持 pending。"""
    with db.write_tx() as conn:
        rec = conn.execute(
            "SELECT id, course_id, requirement_id, status "
            "FROM recognitions WHERE id = ?",
            (recognition_id,),
        ).fetchone()
        if rec is None:
            raise HTTPException(404, f"recognition {recognition_id} not found")
        if rec["status"] != "pending":
            raise HTTPException(
                409,
                f"recognition {recognition_id} is {rec['status']!r}, "
                "only 'pending' can be reviewed",
            )
        qualified = _qualifies(conn, rec["course_id"], rec["requirement_id"])
        if qualified:
            conn.execute(
                "UPDATE recognitions SET status = 'confirmed', "
                "updated_at = datetime('now') WHERE id = ?",
                (recognition_id,),
            )
    return {
        "id": recognition_id,
        "qualified": qualified,
        "status": "confirmed" if qualified else "pending",
    }


@app.delete("/recognitions/{recognition_id}")
def revoke_recognition(recognition_id: int):
    """撤销互认，释放课程与要求两端的占用。"""
    with db.write_tx() as conn:
        rec = conn.execute(
            "SELECT id, course_id, requirement_id, status "
            "FROM recognitions WHERE id = ?",
            (recognition_id,),
        ).fetchone()
        if rec is None:
            raise HTTPException(404, f"recognition {recognition_id} not found")
        conn.execute("DELETE FROM recognitions WHERE id = ?", (recognition_id,))
    return {
        "revoked": recognition_id,
        "released": {"course_id": rec["course_id"], "requirement_id": rec["requirement_id"]},
    }


# ---------------------------------------------------------------- 进度查询


@app.get("/progress")
def get_progress():
    """培养要求进度：区分已认（计分）、待复核（不计分但占用）、空缺。

    全部 SELECT 在同一个只读事务快照中完成，结果只对应某一次提交
    状态，不会读到改派/更正"写到一半"的混合视图。
    """
    requirements = []
    total = earned = 0
    with db.read_tx() as conn:
        reqs = conn.execute(
            "SELECT id, required_credits FROM requirements ORDER BY id"
        ).fetchall()
        for req in reqs:
            total += req["required_credits"]
            rec = conn.execute(
                "SELECT id, course_id, status FROM recognitions "
                "WHERE requirement_id = ? AND status IN ('confirmed', 'pending')",
                (req["id"],),
            ).fetchone()
            if rec is None:
                status, recognition_id, course_id, gained = "vacant", None, None, 0
            elif rec["status"] == "confirmed":
                status, recognition_id, course_id = (
                    "confirmed",
                    rec["id"],
                    rec["course_id"],
                )
                gained = req["required_credits"]
                earned += gained
            else:
                status, recognition_id, course_id, gained = (
                    "pending",
                    rec["id"],
                    rec["course_id"],
                    0,
                )
            requirements.append(
                {
                    "requirement_id": req["id"],
                    "required_credits": req["required_credits"],
                    "status": status,
                    "recognition_id": recognition_id,
                    "course_id": course_id,
                    "earned_credits": gained,
                }
            )
    return {
        "total_required_credits": total,
        "earned_credits": earned,
        "remaining_credits": total - earned,
        "requirements": requirements,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
