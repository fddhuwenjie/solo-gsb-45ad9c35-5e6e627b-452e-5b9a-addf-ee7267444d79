import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with TestClient(app) as c:
        yield c


def seed(client, courses, requirements, rules):
    for cid, credits, score in courses:
        assert client.post(
            "/courses", json={"id": cid, "credits": credits, "score": score}
        ).status_code == 201
    for rid, credits in requirements:
        assert client.post(
            "/requirements", json={"id": rid, "required_credits": credits}
        ).status_code == 201
    for cid, rid, min_score in rules:
        assert client.post(
            "/substitutions",
            json={"course_id": cid, "requirement_id": rid, "min_score": min_score},
        ).status_code == 201


def base_seed(client):
    seed(
        client,
        courses=[("C1", 4, 90), ("C2", 6, 75), ("C3", 2, 95)],
        requirements=[("R1", 4), ("R2", 6)],
        rules=[("C1", "R1", 60), ("C2", "R2", 70), ("C3", "R1", 60)],
    )


# ------------------------------------------------------------ 登记校验


def test_register_validation(client):
    assert client.post(
        "/courses", json={"id": "X", "credits": 0, "score": 80}
    ).status_code == 422
    assert client.post(
        "/courses", json={"id": "X", "credits": 3, "score": 101}
    ).status_code == 422
    assert client.post(
        "/requirements", json={"id": "X", "required_credits": -1}
    ).status_code == 422


def test_register_duplicates_conflict(client):
    base_seed(client)
    assert client.post(
        "/courses", json={"id": "C1", "credits": 4, "score": 90}
    ).status_code == 409
    assert client.post(
        "/requirements", json={"id": "R1", "required_credits": 4}
    ).status_code == 409
    assert client.post(
        "/substitutions",
        json={"course_id": "C1", "requirement_id": "R1", "min_score": 60},
    ).status_code == 409


# ------------------------------------------------------------ 批量互认


def test_batch_confirm_and_progress(client):
    base_seed(client)
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C2", "requirement_id": "R2"},
        ]},
    )
    assert resp.status_code == 201
    assert all(r["status"] == "confirmed" for r in resp.json()["created"])

    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 10
    assert progress["remaining_credits"] == 0
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["R1"]["status"] == "confirmed"
    assert by_req["R2"]["status"] == "confirmed"


def test_batch_all_or_nothing(client):
    base_seed(client)
    # C3 学分 2 < R1 所需 4 -> 整批失败，C2/R2 也不写入
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C2", "requirement_id": "R2"},
            {"course_id": "C3", "requirement_id": "R1"},
        ]},
    )
    assert resp.status_code == 409
    assert client.get("/recognitions").json() == []
    assert client.get("/progress").json()["earned_credits"] == 0


def test_batch_rejects_score_below_min_and_missing_rule(client):
    seed(
        client,
        courses=[("L", 4, 50), ("M", 4, 90)],
        requirements=[("RQ", 4)],
        rules=[("L", "RQ", 60)],  # M 没有规则
    )
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "L", "requirement_id": "RQ"}]}
    )
    assert resp.status_code == 409
    assert "min_score" in resp.json()["detail"][0]
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "M", "requirement_id": "RQ"}]}
    )
    assert resp.status_code == 409
    assert "no substitution rule" in resp.json()["detail"][0]
    assert client.get("/recognitions").json() == []


def test_batch_rejects_duplicates_within_batch(client):
    base_seed(client)
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C1", "requirement_id": "R2"},
        ]},
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "C1", "requirement_id": "R1"},
            {"course_id": "C3", "requirement_id": "R1"},
        ]},
    )
    assert resp.status_code == 409
    assert client.get("/recognitions").json() == []


def test_batch_rejects_conflict_with_existing(client):
    base_seed(client)
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    ).status_code == 201
    # 同一要求被其它课程抢占 -> 409；同一课程复用 -> 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R2"}]}
    )
    assert resp.status_code == 409
    assert len(client.get("/recognitions").json()) == 1


# ------------------------------------------------------------ 更正 -> 待复核 -> 复核/撤销


def test_correction_moves_to_pending_and_keeps_occupation(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    assert client.get("/progress").json()["earned_credits"] == 4

    # 成绩更正到不达标 -> 待复核、停止计分
    resp = client.put("/courses/C1", json={"score": 55})
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == [1]
    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 0
    assert progress["requirements"][0]["status"] == "pending"
    assert client.get("/recognitions", params={"status": "pending"}).json()[0]["id"] == 1

    # 占用保留：其它申请无法抢走该要求或该课程
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R2"}]}
    )
    assert resp.status_code == 409


def test_review_required_to_recount(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    client.put("/courses/C1", json={"score": 55})

    # 复核仍不合格 -> 维持待复核
    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": False, "status": "pending"}
    assert client.get("/progress").json()["earned_credits"] == 0

    # 成绩恢复后不会自动计分，须显式复核
    client.put("/courses/C1", json={"score": 88})
    assert client.get("/recognitions", params={"status": "pending"}).json()[0]["id"] == 1
    assert client.get("/progress").json()["earned_credits"] == 0

    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": True, "status": "confirmed"}
    assert client.get("/progress").json()["earned_credits"] == 4

    # 已认记录不能再复核
    assert client.post("/recognitions/1/review").status_code == 409


def test_revoke_releases_occupation(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    client.put("/courses/C1", json={"score": 40})  # -> pending

    resp = client.delete("/recognitions/1")
    assert resp.status_code == 200
    assert resp.json()["released"] == {"course_id": "C1", "requirement_id": "R1"}
    assert client.get("/recognitions").json() == []
    progress = client.get("/progress").json()
    assert progress["requirements"][0]["status"] == "vacant"

    # 释放后其它课程可以申请该要求
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "C3", "requirement_id": "R1"}]}
    )
    assert resp.status_code == 409  # C3 学分 2 < 4，业务规则拒绝
    seed(client, [("C4", 4, 80)], [], [("C4", "R1", 60)])
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "C4", "requirement_id": "R1"}]}
    ).status_code == 201


def test_correction_of_credits_also_triggers_pending(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    resp = client.put("/courses/C1", json={"credits": 3})  # 学分不再足额
    assert resp.json()["moved_to_pending"] == [1]
    assert client.get("/progress").json()["earned_credits"] == 0


def test_not_found_and_empty_batch(client):
    base_seed(client)
    assert client.put("/courses/NOPE", json={"score": 1}).status_code == 404
    assert client.post("/recognitions/99/review").status_code == 404
    assert client.delete("/recognitions/99").status_code == 404
    assert client.post("/recognitions", json={"items": []}).status_code == 422
    assert client.put("/courses/C1", json={}).status_code == 422


# ------------------------------------------------------------ 整批改派


def reassign_seed(client):
    seed(
        client,
        courses=[("A1", 4, 90), ("A2", 4, 80)],
        requirements=[("RA", 4), ("RB", 4), ("RC", 4)],
        rules=[
            ("A1", "RA", 60),
            ("A1", "RB", 60),
            ("A2", "RB", 60),
            ("A2", "RA", 60),
        ],
    )


def test_reassign_swap_two_requirements(client):
    reassign_seed(client)
    resp = client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "A1", "requirement_id": "RA"},
            {"course_id": "A2", "requirement_id": "RB"},
        ]},
    )
    assert resp.status_code == 201

    # 两条互认互换要求：原占用同时释放，不互相冲突
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "target_requirement_id": "RB"},
            {"recognition_id": 2, "target_requirement_id": "RA"},
        ]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"reassigned": [
        {"id": 1, "from_requirement_id": "RA", "to_requirement_id": "RB"},
        {"id": 2, "from_requirement_id": "RB", "to_requirement_id": "RA"},
    ]}

    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "RB"
    assert recs[1]["status"] == "confirmed"
    assert recs[2]["requirement_id"] == "RA"
    assert recs[1]["id"] == 1 and recs[2]["id"] == 2  # 保留原编号

    by_req = {
        r["requirement_id"]: r
        for r in client.get("/progress").json()["requirements"]
    }
    assert by_req["RA"]["recognition_id"] == 2
    assert by_req["RB"]["recognition_id"] == 1
    assert by_req["RC"]["status"] == "vacant"
    assert client.get("/progress").json()["earned_credits"] == 8


def test_reassign_single_move_and_then_correction_uses_new_relation(client):
    reassign_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "A1", "requirement_id": "RA"}]}
    )
    # A1 只有 RA/RB 的替代规则且最低 60，改派到不存在规则的 RC 必须失败
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RC"}]},
    )
    assert resp.status_code == 409
    assert "no substitution rule" in resp.json()["detail"][0]
    # 整批保持不变
    rec = client.get("/recognitions").json()[0]
    assert rec["requirement_id"] == "RA"

    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RB"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["reassigned"] == [
        {"id": 1, "from_requirement_id": "RA", "to_requirement_id": "RB"}
    ]

    # 改派后更正使新课程关系不合格 -> 按改派后的关系转待复核
    resp = client.put("/courses/A1", json={"score": 55})
    assert resp.json()["moved_to_pending"] == [1]
    by_req = {
        r["requirement_id"]: r
        for r in client.get("/progress").json()["requirements"]
    }
    assert by_req["RA"]["status"] == "vacant"
    assert by_req["RB"]["status"] == "pending"


def test_reassign_all_or_nothing_keeps_originals(client):
    reassign_seed(client)
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "A1", "requirement_id": "RA"},
            {"course_id": "A2", "requirement_id": "RB"},
        ]},
    )
    # 第二条目标 RA 是批内记录 1 的占用（互换本应成功），但第一条 RC
    # 不存在规则 -> 整批拒绝，第二条也不得移动
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "target_requirement_id": "RC"},
            {"recognition_id": 2, "target_requirement_id": "RA"},
        ]},
    )
    assert resp.status_code == 409
    detail = " ".join(resp.json()["detail"])
    assert "no substitution rule" in detail

    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "RA"
    assert recs[2]["requirement_id"] == "RB"
    assert client.get("/progress").json()["earned_credits"] == 8


def test_reassign_rejects_pending_source(client):
    reassign_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "A1", "requirement_id": "RA"}]}
    )
    client.put("/courses/A1", json={"score": 50})  # -> pending
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RB"}]},
    )
    assert resp.status_code == 409
    assert "only 'confirmed' can be reassigned" in resp.json()["detail"][0]
    rec = client.get("/recognitions").json()[0]
    assert rec["requirement_id"] == "RA" and rec["status"] == "pending"


def test_reassign_rejects_target_occupied_outside_batch_confirmed(client):
    reassign_seed(client)
    # 记录 1: A1->RA，记录 2: A2->RB；记录 1 想改派到 RB，
    # 但 RB 被批外记录 2 占用 -> 拒绝
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "A1", "requirement_id": "RA"},
            {"course_id": "A2", "requirement_id": "RB"},
        ]},
    )
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RB"}]},
    )
    assert resp.status_code == 409
    assert "already occupied" in resp.json()["detail"][0]
    recs = {r["id"]: r for r in client.get("/recognitions").json()}
    assert recs[1]["requirement_id"] == "RA"
    assert recs[2]["requirement_id"] == "RB"


def test_reassign_rejects_target_occupied_outside_batch_pending(client):
    reassign_seed(client)
    # A1->RA（已认），A2->RB 后因成绩更正变待复核但仍占用 RB
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "A1", "requirement_id": "RA"},
            {"course_id": "A2", "requirement_id": "RB"},
        ]},
    )
    client.put("/courses/A2", json={"score": 50})
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RB"}]},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"][0]
    assert "already occupied" in detail and "pending" in detail
    assert client.get("/recognitions").json()[0]["requirement_id"] == "RA"


def test_reassign_validates_current_score_and_credits(client):
    seed(
        client,
        courses=[("S1", 4, 55)],
        requirements=[("RQ1", 4), ("RQ2", 4)],
        rules=[("S1", "RQ1", 60), ("S1", "RQ2", 60)],
    )
    # S1 当前成绩 55、S2 学分 3 < 4：先无法申请；修正后再认
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "S1", "requirement_id": "RQ1"}]}
    ).status_code == 409
    client.put("/courses/S1", json={"score": 88})
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "S1", "requirement_id": "RQ1"}]}
    ).status_code == 201

    # 改派核对的是课程"当前"成绩/学分：成绩再次跌落后改派被拒
    client.put("/courses/S1", json={"score": 55})  # 已认记录转待复核
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RQ2"}]},
    )
    # 先因来源已是待复核被拒
    assert resp.status_code == 409
    client.put("/courses/S1", json={"score": 90})
    client.post("/recognitions/1/review")
    client.put("/courses/S1", json={"credits": 2})  # 复核合格后学分再缩水 -> pending
    client.put("/courses/S1", json={"credits": 4})
    client.post("/recognitions/1/review")  # 重新合格
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RQ2"}]},
    )
    assert resp.status_code == 200


def test_reassign_input_validation(client):
    reassign_seed(client)
    assert client.post("/recognitions/reassign", json={"items": []}).status_code == 422
    client.post(
        "/recognitions", json={"items": [{"course_id": "A1", "requirement_id": "RA"}]}
    )
    # 互认编号重复
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "target_requirement_id": "RB"},
            {"recognition_id": 1, "target_requirement_id": "RC"},
        ]},
    )
    assert resp.status_code == 409
    assert "duplicated in batch" in resp.json()["detail"][0]
    # 目标要求重复
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "target_requirement_id": "RB"},
        ]},
    )
    assert resp.status_code == 200
    assert client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "target_requirement_id": "RA"},
        ]},
    ).status_code == 200  # 可再改派回原要求（占用已在批内让出）
    assert client.post(
        "/recognitions/reassign",
        json={"items": [
            {"recognition_id": 1, "target_requirement_id": "RB"},
            {"recognition_id": 99, "target_requirement_id": "RC"},
        ]},
    ).status_code == 409  # 编号不存在整批拒绝
    # 目标要求不存在
    assert client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "NOPE"}]},
    ).status_code == 409


def test_reassign_then_review_and_revoke_follow_new_relation(client):
    reassign_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "A1", "requirement_id": "RA"}]}
    )
    assert client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RB"}]},
    ).status_code == 200
    client.put("/courses/A1", json={"score": 50})  # 按新关系 RB 转待复核
    assert client.post("/recognitions/1/review").json()["qualified"] is False
    client.put("/courses/A1", json={"score": 90})
    assert client.post("/recognitions/1/review").json()["status"] == "confirmed"

    # 撤销释放的是改派后的要求 RB
    resp = client.delete("/recognitions/1")
    assert resp.json()["released"] == {"course_id": "A1", "requirement_id": "RB"}
    by_req = {
        r["requirement_id"]: r
        for r in client.get("/progress").json()["requirements"]
    }
    assert by_req["RA"]["status"] == "vacant"
    assert by_req["RB"]["status"] == "vacant"


def test_concurrent_reassign_serialized(client):
    reassign_seed(client)
    # 并发中还要把记录 1 移向空闲 RC，补一条 A1->RC 替代规则
    seed(client, [], [], [("A1", "RC", 60)])
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "A1", "requirement_id": "RA"},
            {"course_id": "A2", "requirement_id": "RB"},
        ]},
    )

    def move(i):
        with TestClient(app) as c:
            # 一半互换、一半移向空闲 RC；两类提交并发执行
            if i % 2:
                return c.post(
                    "/recognitions/reassign",
                    json={"items": [
                        {"recognition_id": 1, "target_requirement_id": "RB"},
                        {"recognition_id": 2, "target_requirement_id": "RA"},
                    ]},
                ).status_code
            return c.post(
                "/recognitions/reassign",
                json={"items": [
                    {"recognition_id": 1, "target_requirement_id": "RC"},
                ]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(move, range(16)))

    # 每次串行化的改派本身都自洽；最终每条要求至多一条占用记录
    assert all(code == 200 for code in codes)
    recs = client.get("/recognitions").json()
    occupied = [r["requirement_id"] for r in recs if r["status"] == "confirmed"]
    assert len(occupied) == len(set(occupied)) == 2
    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 8
    assert progress["remaining_credits"] == 4  # RC 等总需 12


# ------------------------------------------------------------ 培养方案修订


def revision_seed(client):
    seed(
        client,
        courses=[("P1", 4, 90), ("P2", 4, 65), ("P3", 2, 95)],
        requirements=[("RP1", 4), ("RP2", 4)],
        rules=[("P1", "RP1", 60), ("P2", "RP2", 60), ("P3", "RP1", 60)],
    )


def test_plan_revision_rechecks_confirmed_records(client):
    revision_seed(client)
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "P1", "requirement_id": "RP1"},
            {"course_id": "P2", "requirement_id": "RP2"},
        ]},
    )
    assert client.get("/progress").json()["earned_credits"] == 8

    # RP1 所需学分 4 -> 5（P1 学分 4 不再足额）；
    # P2/RP2 最低成绩 60 -> 70（P2 成绩 65 不再达标）
    resp = client.post(
        "/plan-revisions",
        json={
            "requirement_changes": [
                {"requirement_id": "RP1", "required_credits": 5}
            ],
            "substitution_changes": [
                {"course_id": "P2", "requirement_id": "RP2", "min_score": 70}
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "revised": {
            "requirements": [{"requirement_id": "RP1", "required_credits": 5}],
            "substitutions": [
                {"course_id": "P2", "requirement_id": "RP2", "min_score": 70.0}
            ],
        },
        "moved_to_pending": [1, 2],
    }

    progress = client.get("/progress").json()
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["RP1"]["status"] == "pending"
    assert by_req["RP2"]["status"] == "pending"
    assert progress["total_required_credits"] == 9  # 已认进度按新学分计算
    assert progress["earned_credits"] == 0
    assert client.get("/requirements").json()[0]["required_credits"] == 5
    assert client.get("/substitutions").json()[1]["min_score"] == 70


def test_plan_revision_still_qualified_recounts_with_new_credits(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    # 放宽要求学分：4 -> 3，P1 仍合格；已认进度按新学分计 3
    resp = client.post(
        "/plan-revisions",
        json={
            "requirement_changes": [{"requirement_id": "RP1", "required_credits": 3}]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == []
    progress = client.get("/progress").json()
    assert progress["total_required_credits"] == 7
    assert progress["earned_credits"] == 3
    assert client.get("/recognitions").json()[0]["status"] == "confirmed"

    # 再调高规则最低成绩但仍低于 P1 的 90 -> 仍合格
    resp = client.post(
        "/plan-revisions",
        json={
            "substitution_changes": [
                {"course_id": "P1", "requirement_id": "RP1", "min_score": 85}
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == []
    assert client.get("/progress").json()["earned_credits"] == 3


def test_plan_revision_keeps_both_end_occupations(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    resp = client.post(
        "/plan-revisions",
        json={
            "requirement_changes": [{"requirement_id": "RP1", "required_credits": 5}]
        },
    )
    assert resp.json()["moved_to_pending"] == [1]

    # 停止计分但继续占用两端：其它课程无法抢要求，该课程无法申请它处
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "P3", "requirement_id": "RP1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP2"}]}
    )
    assert resp.status_code == 409


def test_plan_revision_pending_requires_explicit_review(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P2", "requirement_id": "RP2"}]}
    )
    # 提高门槛令 P2 失去资格 -> 待复核
    client.post(
        "/plan-revisions",
        json={
            "substitution_changes": [
                {"course_id": "P2", "requirement_id": "RP2", "min_score": 80}
            ]
        },
    )
    assert client.get("/progress").json()["earned_credits"] == 0

    # 重新放宽到达标：原本待复核的记录不自动计分，须显式复核
    resp = client.post(
        "/plan-revisions",
        json={
            "substitution_changes": [
                {"course_id": "P2", "requirement_id": "RP2", "min_score": 60}
            ]
        },
    )
    assert resp.json()["moved_to_pending"] == []
    assert client.get("/progress").json()["earned_credits"] == 0
    assert (
        client.get("/recognitions", params={"status": "pending"}).json()[0]["id"] == 1
    )
    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": True, "status": "confirmed"}
    assert client.get("/progress").json()["earned_credits"] == 4


def test_plan_revision_all_or_nothing_missing_reference(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    # 一项合法、一项引用不存在的规则 -> 整次不生效
    resp = client.post(
        "/plan-revisions",
        json={
            "requirement_changes": [{"requirement_id": "RP1", "required_credits": 3}],
            "substitution_changes": [
                {"course_id": "P1", "requirement_id": "NOPE", "min_score": 50}
            ],
        },
    )
    assert resp.status_code == 409
    assert "no substitution rule" in resp.json()["detail"][0]

    # 要求学分未改、互认未转待复核、进度不变
    reqs = {r["id"]: r for r in client.get("/requirements").json()}
    assert reqs["RP1"]["required_credits"] == 4
    assert client.get("/recognitions").json()[0]["status"] == "confirmed"
    assert client.get("/progress").json()["earned_credits"] == 4

    # 引用不存在的要求同样整次拒绝
    resp = client.post(
        "/plan-revisions",
        json={
            "requirement_changes": [
                {"requirement_id": "NOPE", "required_credits": 3}
            ]
        },
    )
    assert resp.status_code == 409
    assert "not found" in resp.json()["detail"][0]


def test_plan_revision_rejects_duplicates_and_empty(client):
    revision_seed(client)
    # 同一要求重复
    resp = client.post(
        "/plan-revisions",
        json={
            "requirement_changes": [
                {"requirement_id": "RP1", "required_credits": 3},
                {"requirement_id": "RP1", "required_credits": 5},
            ]
        },
    )
    assert resp.status_code == 409
    assert "duplicated in batch" in resp.json()["detail"][0]
    # 同一替代规则重复
    resp = client.post(
        "/plan-revisions",
        json={
            "substitution_changes": [
                {"course_id": "P1", "requirement_id": "RP1", "min_score": 50},
                {"course_id": "P1", "requirement_id": "RP1", "min_score": 80},
            ]
        },
    )
    assert resp.status_code == 409
    assert "duplicated in batch" in resp.json()["detail"][0]
    # 两项均缺省 -> 没有任何改动
    assert client.post("/plan-revisions", json={}).status_code == 422
    # 数值无效 -> 422
    assert client.post(
        "/plan-revisions",
        json={"requirement_changes": [{"requirement_id": "RP1", "required_credits": 0}]},
    ).status_code == 422
    assert client.post(
        "/plan-revisions",
        json={
            "substitution_changes": [
                {"course_id": "P1", "requirement_id": "RP1", "min_score": 101}
            ]
        },
    ).status_code == 422
    # 被拒后数据原样
    reqs = {r["id"]: r for r in client.get("/requirements").json()}
    assert reqs["RP1"]["required_credits"] == 4
    rules = client.get("/substitutions").json()
    assert all(r["min_score"] == 60 for r in rules)


def test_plan_revision_new_standard_governs_apply_and_reassign(client):
    seed(
        client,
        courses=[("Q1", 6, 90), ("Q2", 4, 80)],
        requirements=[("RQ1", 4), ("RQ2", 4)],
        rules=[("Q1", "RQ1", 60), ("Q1", "RQ2", 60), ("Q2", "RQ2", 60)],
    )
    client.post(
        "/recognitions", json={"items": [{"course_id": "Q1", "requirement_id": "RQ1"}]}
    )
    # 新标准：RQ1 需要 5 学分、Q1/RQ1 最低成绩 95 -> 互认 1 转待复核
    resp = client.post(
        "/plan-revisions",
        json={
            "requirement_changes": [{"requirement_id": "RQ1", "required_credits": 5}],
            "substitution_changes": [
                {"course_id": "Q1", "requirement_id": "RQ1", "min_score": 95}
            ],
        },
    )
    assert resp.json()["moved_to_pending"] == [1]

    # 后续申请按新标准判断：Q2 学分 4 < 5，无法认 RQ1（且 RQ1 仍被占用）
    assert client.post(
        "/recognitions", json={"items": [{"course_id": "Q2", "requirement_id": "RQ1"}]}
    ).status_code == 409

    # Q1 改派 RQ2：待复核来源不可改派；显式复核也无法恢复
    # （Q1 成绩 90 < 95 且 RQ1 现需 5 学分，Q1 学分 6 已足额）
    assert client.post("/recognitions/1/review").json()["qualified"] is False

    # 放宽 RQ1 规则成绩后显式复核恢复，再改派到 RQ2 按新标准核对
    # （RQ2 学分仍为 4、Q1/RQ2 最低 60，合格）
    client.post(
        "/plan-revisions",
        json={
            "substitution_changes": [
                {"course_id": "Q1", "requirement_id": "RQ1", "min_score": 90}
            ]
        },
    )
    assert client.post("/recognitions/1/review").json()["status"] == "confirmed"
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RQ2"}]},
    )
    assert resp.status_code == 200

    # 改派后再提高 RQ2 门槛令 Q1 不合格 -> 按新标准转待复核
    resp = client.post(
        "/plan-revisions",
        json={
            "substitution_changes": [
                {"course_id": "Q1", "requirement_id": "RQ2", "min_score": 95}
            ]
        },
    )
    assert resp.json()["moved_to_pending"] == [1]
    by_req = {
        r["requirement_id"]: r
        for r in client.get("/progress").json()["requirements"]
    }
    assert by_req["RQ2"]["status"] == "pending"


def test_concurrent_plan_revision_serialized(client):
    revision_seed(client)
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "P1", "requirement_id": "RP1"},
            {"course_id": "P2", "requirement_id": "RP2"},
        ]},
    )

    def write(i):
        with TestClient(app) as c:
            if i % 2:
                # 反复横跳 P1/RP1 最低成绩：95（不合格）与 60（合格）
                return c.post(
                    "/plan-revisions",
                    json={
                        "substitution_changes": [
                            {
                                "course_id": "P1",
                                "requirement_id": "RP1",
                                "min_score": 95 if i % 4 == 1 else 60,
                            }
                        ]
                    },
                ).status_code
            return c.put("/courses/P2", json={"score": 50 if i % 4 == 2 else 90}).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(write, range(20)))

    # 串行化后每个写操作自洽：状态只能是 confirmed/pending，占用始终唯一
    assert all(code == 200 for code in codes)
    recs = client.get("/recognitions").json()
    assert len(recs) == 2
    assert {r["status"] for r in recs} <= {"confirmed", "pending"}
    reqs_occupied = [r["requirement_id"] for r in recs]
    assert len(set(reqs_occupied)) == 2


# ------------------------------------------------------------ 并发


def test_concurrent_batches_no_double_use(client):
    base_seed(client)

    def submit(i):
        with TestClient(app) as c:
            course = "C1" if i % 2 == 0 else "C3"
            return c.post(
                "/recognitions",
                json={"items": [{"course_id": course, "requirement_id": "R1"}]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(submit, range(16)))

    assert codes.count(201) == 1
    assert codes.count(409) == 15
    recs = client.get("/recognitions").json()
    assert len(recs) == 1
    assert client.get("/progress").json()["earned_credits"] == 4


def test_concurrent_correction_and_submit_consistent(client):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )

    def correct(i):
        with TestClient(app) as c:
            return c.put("/courses/C1", json={"score": 50 if i % 2 else 95}).status_code

    def submit(_):
        with TestClient(app) as c:
            return c.post(
                "/recognitions",
                json={"items": [{"course_id": "C3", "requirement_id": "R1"}]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda i: correct(i) if i % 2 else submit(i), range(20)))

    assert all(code in (200, 409) for code in results)
    # 无论交错顺序如何，R1 始终只有一条占用记录
    recs = client.get("/recognitions").json()
    assert len([r for r in recs if r["requirement_id"] == "R1"]) == 1


# ------------------------------------------------------------ 持久化


def test_records_survive_restart(client, tmp_path, monkeypatch):
    base_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "C1", "requirement_id": "R1"}]}
    )
    # 模拟重启：重新初始化（幂等）并新建客户端，同一数据库文件
    db.init_db()
    with TestClient(app) as c2:
        assert len(c2.get("/recognitions").json()) == 1
        assert c2.get("/progress").json()["earned_credits"] == 4
