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
        courses=[("P1", 4, 90), ("P2", 6, 75)],
        requirements=[("RP1", 4), ("RP2", 6)],
        rules=[("P1", "RP1", 60), ("P2", "RP2", 70)],
    )


def test_revision_requirement_credits_moves_disqualified(client):
    revision_seed(client)
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "P1", "requirement_id": "RP1"},
            {"course_id": "P2", "requirement_id": "RP2"},
        ]},
    )
    assert client.get("/progress").json()["earned_credits"] == 10

    # RP1 所需学分 4 -> 6：P1 只有 4 学分，互认 1 转待复核；RP2 保持已认，
    # 进度按新学分计算（RP2 的 6 分仍计；RP1 停止计分）
    resp = client.post(
        "/program/revisions",
        json={"requirement_changes": [
            {"requirement_id": "RP1", "required_credits": 6},
        ]},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "revised": {
            "requirements": [{"requirement_id": "RP1", "required_credits": 6}],
            "substitutions": [],
        },
        "moved_to_pending": [1],
    }
    progress = client.get("/progress").json()
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["RP1"]["status"] == "pending"
    assert by_req["RP1"]["required_credits"] == 6
    assert by_req["RP1"]["earned_credits"] == 0
    assert by_req["RP2"]["status"] == "confirmed"
    assert progress["total_required_credits"] == 12
    assert progress["earned_credits"] == 6
    assert client.get("/requirements").json()[0]["required_credits"] == 6


def test_revision_min_score_moves_disqualified(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P2", "requirement_id": "RP2"}]}
    )
    # P2 成绩 75：最低成绩提到 80 即失去资格
    resp = client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "P2", "requirement_id": "RP2", "min_score": 80},
        ]},
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == [1]
    assert resp.json()["revised"]["substitutions"] == [
        {"course_id": "P2", "requirement_id": "RP2", "min_score": 80.0}
    ]
    assert client.get("/substitutions").json()[1]["min_score"] == 80.0
    assert client.get("/progress").json()["earned_credits"] == 0


def test_revision_mixed_changes_apply_together(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    # 一次修订同时改两类：RP1 学分维持 4（仍足额），但最低成绩提到 95 -> 失去资格
    resp = client.post(
        "/program/revisions",
        json={
            "requirement_changes": [{"requirement_id": "RP1", "required_credits": 4}],
            "substitution_changes": [
                {"course_id": "P1", "requirement_id": "RP1", "min_score": 95}
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == [1]
    # 两类改动都已落库
    rules = {(s["course_id"], s["requirement_id"]): s["min_score"]
             for s in client.get("/substitutions").json()}
    assert rules[("P1", "RP1")] == 95.0
    assert {r["id"]: r["required_credits"]
            for r in client.get("/requirements").json()}["RP1"] == 4


def test_revision_qualifying_changes_keep_confirmed_and_recompute_progress(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    # 降低学分要求：仍合格，状态不变；进度按新学分（3）计算
    resp = client.post(
        "/program/revisions",
        json={"requirement_changes": [
            {"requirement_id": "RP1", "required_credits": 3},
        ]},
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == []
    progress = client.get("/progress").json()
    by_req = {r["requirement_id"]: r for r in progress["requirements"]}
    assert by_req["RP1"]["status"] == "confirmed"
    assert by_req["RP1"]["earned_credits"] == 3
    assert progress["earned_credits"] == 3
    assert progress["remaining_credits"] == 6  # RP1 需 3 + RP2 需 6


def test_revision_pending_keeps_occupation(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    client.post(
        "/program/revisions",
        json={"requirement_changes": [
            {"requirement_id": "RP1", "required_credits": 6},
        ]},
    )
    # 转待复核后两端占用保留：其它课程/要求的新申请仍被挡住
    seed(client, [("P3", 6, 99)], [], [("P3", "RP1", 60)])
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "P3", "requirement_id": "RP1"}]}
    )
    assert resp.status_code == 409
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP2"}]}
    )
    assert resp.status_code == 409


def test_revision_loosening_does_not_auto_restore_pending(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    # 收紧 -> 转待复核；再放宽回原标准，待复核记录不得自动恢复
    client.post(
        "/program/revisions",
        json={"requirement_changes": [
            {"requirement_id": "RP1", "required_credits": 6},
        ]},
    )
    resp = client.post(
        "/program/revisions",
        json={
            "requirement_changes": [{"requirement_id": "RP1", "required_credits": 4}],
            "substitution_changes": [
                {"course_id": "P1", "requirement_id": "RP1", "min_score": 60}
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == []
    rec = client.get("/recognitions").json()[0]
    assert rec["status"] == "pending"
    assert client.get("/progress").json()["earned_credits"] == 0
    # 显式复核按新标准判断 -> 恢复计分
    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": True, "status": "confirmed"}
    assert client.get("/progress").json()["earned_credits"] == 4


def test_revision_review_applies_new_standard(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    # 把最低成绩提到 95：P1 的 90 分不达标 -> 待复核
    client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "P1", "requirement_id": "RP1", "min_score": 95},
        ]},
    )
    # 复核按新标准（95）：仍不合格，维持待复核
    resp = client.post("/recognitions/1/review")
    assert resp.json() == {"id": 1, "qualified": False, "status": "pending"}
    # 标准再放宽到 80：显式复核才恢复
    client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "P1", "requirement_id": "RP1", "min_score": 80},
        ]},
    )
    resp = client.post("/recognitions/1/review")
    assert resp.json()["status"] == "confirmed"


def test_revision_new_submissions_use_new_standard(client):
    revision_seed(client)
    # 提高 RP2 的学分门槛到 8：P2 只有 6 学分，之后的新申请被拒
    resp = client.post(
        "/program/revisions",
        json={"requirement_changes": [
            {"requirement_id": "RP2", "required_credits": 8},
        ]},
    )
    assert resp.status_code == 200
    resp = client.post(
        "/recognitions", json={"items": [{"course_id": "P2", "requirement_id": "RP2"}]}
    )
    assert resp.status_code == 409
    assert "credits" in resp.json()["detail"][0]
    assert client.get("/recognitions").json() == []


def test_revision_then_reassign_uses_new_standard(client):
    seed(
        client,
        courses=[("Q1", 4, 70)],
        requirements=[("RQA", 4), ("RQB", 4)],
        rules=[("Q1", "RQA", 60), ("Q1", "RQB", 60)],
    )
    client.post(
        "/recognitions", json={"items": [{"course_id": "Q1", "requirement_id": "RQA"}]}
    )
    # 修订把 Q1->RQB 的最低成绩提到 80：Q1 成绩 70，改派到 RQB 须按新标准拒绝
    resp = client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "Q1", "requirement_id": "RQB", "min_score": 80},
        ]},
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == []  # 现有互认走的是 RQA（仍是 60）
    resp = client.post(
        "/recognitions/reassign",
        json={"items": [{"recognition_id": 1, "target_requirement_id": "RQB"}]},
    )
    assert resp.status_code == 409
    assert "min_score" in resp.json()["detail"][0]
    rec = client.get("/recognitions").json()[0]
    assert rec["requirement_id"] == "RQA" and rec["status"] == "confirmed"


def test_revision_unknown_reference_aborts_whole_batch(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )
    # 要求不存在 -> 整次不生效，同批的规则改动也不落库
    resp = client.post(
        "/program/revisions",
        json={
            "requirement_changes": [{"requirement_id": "NOPE", "required_credits": 9}],
            "substitution_changes": [
                {"course_id": "P1", "requirement_id": "RP1", "min_score": 95}
            ],
        },
    )
    assert resp.status_code == 409
    assert "not found" in resp.json()["detail"][0]
    rules = {(s["course_id"], s["requirement_id"]): s["min_score"]
             for s in client.get("/substitutions").json()}
    assert rules[("P1", "RP1")] == 60
    assert client.get("/recognitions").json()[0]["status"] == "confirmed"
    assert client.get("/progress").json()["earned_credits"] == 4

    # 替代规则不存在 -> 整次不生效
    resp = client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "P1", "requirement_id": "RP2", "min_score": 50},
        ]},
    )
    assert resp.status_code == 409
    assert "not found" in resp.json()["detail"][0]


def test_revision_rejects_duplicates(client):
    revision_seed(client)
    resp = client.post(
        "/program/revisions",
        json={"requirement_changes": [
            {"requirement_id": "RP1", "required_credits": 3},
            {"requirement_id": "RP1", "required_credits": 5},
        ]},
    )
    assert resp.status_code == 409
    assert "duplicated in batch" in resp.json()["detail"][0]
    resp = client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "P1", "requirement_id": "RP1", "min_score": 50},
            {"course_id": "P1", "requirement_id": "RP1", "min_score": 80},
        ]},
    )
    assert resp.status_code == 409
    assert "duplicated in batch" in resp.json()["detail"][0]
    # 全部保持原值
    assert client.get("/requirements").json()[0]["required_credits"] == 4
    rules = {(s["course_id"], s["requirement_id"]): s["min_score"]
             for s in client.get("/substitutions").json()}
    assert rules[("P1", "RP1")] == 60


def test_revision_input_validation(client):
    revision_seed(client)
    # 空批次 / 两类改动都缺
    assert client.post("/program/revisions", json={}).status_code == 422
    assert client.post("/program/revisions", json={"requirement_changes": []}).status_code == 422
    # 学分必须正整数
    assert client.post(
        "/program/revisions",
        json={"requirement_changes": [{"requirement_id": "RP1", "required_credits": 0}]},
    ).status_code == 422
    # 最低成绩越界
    assert client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "P1", "requirement_id": "RP1", "min_score": 101},
        ]},
    ).status_code == 422
    assert client.post(
        "/program/revisions",
        json={"substitution_changes": [
            {"course_id": "P1", "requirement_id": "RP1", "min_score": -1},
        ]},
    ).status_code == 422


def test_revision_returns_multiple_moved_sorted(client):
    revision_seed(client)
    client.post(
        "/recognitions",
        json={"items": [
            {"course_id": "P1", "requirement_id": "RP1"},
            {"course_id": "P2", "requirement_id": "RP2"},
        ]},
    )
    # 两项已认都因新标准失去资格 -> 编号按互认 id 升序返回
    resp = client.post(
        "/program/revisions",
        json={
            "requirement_changes": [{"requirement_id": "RP1", "required_credits": 9}],
            "substitution_changes": [
                {"course_id": "P2", "requirement_id": "RP2", "min_score": 90}
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["moved_to_pending"] == [1, 2]
    pending = client.get("/recognitions", params={"status": "pending"}).json()
    assert [r["id"] for r in pending] == [1, 2]
    assert client.get("/progress").json()["earned_credits"] == 0


def test_concurrent_revision_serialized(client):
    revision_seed(client)
    client.post(
        "/recognitions", json={"items": [{"course_id": "P1", "requirement_id": "RP1"}]}
    )

    def op(i):
        with TestClient(app) as c:
            if i % 2:
                # 收紧：P1(4 学分) 将失去资格
                return c.post(
                    "/program/revisions",
                    json={"requirement_changes": [
                        {"requirement_id": "RP1", "required_credits": 6},
                    ]},
                ).status_code
            # 放宽：保持合格
            return c.post(
                "/program/revisions",
                json={"requirement_changes": [
                    {"requirement_id": "RP1", "required_credits": 4},
                ]},
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(op, range(16)))

    # 每次修订串行提交，均自洽成功；收紧一旦执行，互认即转待复核，
    # 之后的放宽修订不会自动恢复它（须显式复核），因此最终必为待复核；
    # RP1 始终至多一条占用记录，不会出现重复使用或中间态。
    assert all(code == 200 for code in codes)
    recs = client.get("/recognitions").json()
    rp1_recs = [r for r in recs if r["requirement_id"] == "RP1"]
    assert len(rp1_recs) == 1 and rp1_recs[0]["status"] == "pending"
    progress = client.get("/progress").json()
    assert progress["earned_credits"] == 0
    assert progress["requirements"] and all(
        r["status"] in ("vacant", "pending", "confirmed")
        for r in progress["requirements"]
    )


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
