# 学分互认 API

用 FastAPI + SQLite 实现的外校课程学分互认服务。

## 业务规则

- **登记**：外校课程（编号、正整数学分、0~100 分成绩）、本校要求（编号、所需学分）、课程可替代的要求与最低成绩。
- **批量互认**：一次提交多条，仅当每条的外课学分足额且成绩达标、源课与要求在本批及现存"已认/待复核"记录中均未重复时，**整批确认**并按要求学分计入进度；否则**整批不写**（409 并返回全部原因）。
- **成绩更正**：更正后不再合格的"已认"互认原子地转为**待复核**，停止计分，但课程与要求两端的占用保留。
- **复核**：成绩恢复后须显式复核才能重新计分；复核仍不合格则维持待复核。
- **撤销**：删除互认记录，释放两端占用，之后可重新申请。
- **整批改派**：一次把多条**已认**互认改派到各自的目标要求（`POST /recognitions/reassign`）。按本批原占用**同时释放后**的状态核对：互认编号与目标要求在批内均不得重复、来源须是现存已认记录（待复核不可改派）、课程对目标要求的替代规则/当前成绩/当前学分均合格、目标要求未被批外的已认或待复核记录占用（两条记录互换要求不会相互冲突）。任一项不合格则**原互认与进度整批不变**；成功则保留原互认编号、更新要求与进度。
- **查询**：`/progress` 区分已认（计分）、待复核（不计分但占用）、空缺；一次查询取同一提交快照，不会混出改派前后的结果。
- **并发**：所有写操作（申请、成绩更正、复核、撤销、改派）在进程锁 + `BEGIN IMMEDIATE` 事务内串行执行，检查与写入原子完成，并发操作不会重复使用或暴露中间态；改派之后的操作一律按改派后的关系判断。

## 启动

```bash
docker compose up --build
```

服务监听 **8000** 端口，交互式文档见 <http://localhost:8000/docs>。
SQLite 数据库写入命名卷 `credit_data`（容器内 `/data/app.db`），首次启动自动建表，**重启后记录仍在**。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000   # 数据库默认在 ./data/app.db，可用 DB_PATH 覆盖
pytest                             # 运行测试
```

## HTTP 示例

```bash
# 1. 登记外校课程：编号、学分、成绩
curl -s -X POST localhost:8000/courses \
  -H 'Content-Type: application/json' \
  -d '{"id": "C1", "credits": 4, "score": 90}'

# 2. 登记本校要求：编号、所需学分
curl -s -X POST localhost:8000/requirements \
  -H 'Content-Type: application/json' \
  -d '{"id": "R1", "required_credits": 4}'

# 3. 登记替代规则：C1 可替代 R1，最低成绩 60
curl -s -X POST localhost:8000/substitutions \
  -H 'Content-Type: application/json' \
  -d '{"course_id": "C1", "requirement_id": "R1", "min_score": 60}'

# 4. 批量提交互认（整批确认或整批拒绝）
curl -s -X POST localhost:8000/recognitions \
  -H 'Content-Type: application/json' \
  -d '{"items": [{"course_id": "C1", "requirement_id": "R1"}]}'

# 5. 查询进度：已认 / 待复核 / 空缺
curl -s localhost:8000/progress

# 6. 成绩更正到不达标 -> 互认转为待复核，停止计分但保留占用
curl -s -X PUT localhost:8000/courses/C1 \
  -H 'Content-Type: application/json' -d '{"score": 55}'
curl -s 'localhost:8000/recognitions?status=pending'

# 7. 成绩恢复后须显式复核才重新计分（复核仍不合格则维持待复核）
curl -s -X PUT localhost:8000/courses/C1 \
  -H 'Content-Type: application/json' -d '{"score": 88}'
curl -s -X POST localhost:8000/recognitions/1/review

# 8. 撤销互认，释放课程与要求的占用
curl -s -X DELETE localhost:8000/recognitions/1

# 9. 整批改派：两门已认课程互换培养要求
#    （逐条撤销再申请会暂时放出名额，互换还会因相互占用而失败；整批改派一次完成）
#    下列编号独立，可在全新数据库上直接执行；互认编号以第 9 步批量互认返回的 id 为准
curl -s -X POST localhost:8000/courses -H 'Content-Type: application/json' \
  -d '{"id": "D1", "credits": 4, "score": 86}'
curl -s -X POST localhost:8000/courses -H 'Content-Type: application/json' \
  -d '{"id": "D2", "credits": 4, "score": 78}'
curl -s -X POST localhost:8000/requirements -H 'Content-Type: application/json' \
  -d '{"id": "RD1", "required_credits": 4}'
curl -s -X POST localhost:8000/requirements -H 'Content-Type: application/json' \
  -d '{"id": "RD2", "required_credits": 4}'
curl -s -X POST localhost:8000/substitutions -H 'Content-Type: application/json' \
  -d '{"course_id": "D1", "requirement_id": "RD1", "min_score": 60}'
curl -s -X POST localhost:8000/substitutions -H 'Content-Type: application/json' \
  -d '{"course_id": "D1", "requirement_id": "RD2", "min_score": 60}'
curl -s -X POST localhost:8000/substitutions -H 'Content-Type: application/json' \
  -d '{"course_id": "D2", "requirement_id": "RD1", "min_score": 60}'
curl -s -X POST localhost:8000/substitutions -H 'Content-Type: application/json' \
  -d '{"course_id": "D2", "requirement_id": "RD2", "min_score": 60}'
curl -s -X POST localhost:8000/recognitions -H 'Content-Type: application/json' \
  -d '{"items": [{"course_id": "D1", "requirement_id": "RD1"}, {"course_id": "D2", "requirement_id": "RD2"}]}'
# 全新数据库上两条互认编号为 1、2（若跑过前面的示例则编号顺延，按实际 id 替换）
curl -s -X POST localhost:8000/recognitions/reassign -H 'Content-Type: application/json' \
  -d '{"items": [{"recognition_id": 1, "target_requirement_id": "RD2"}, {"recognition_id": 2, "target_requirement_id": "RD1"}]}'
# -> {"reassigned":[{"id":1,"from_requirement_id":"RD1","to_requirement_id":"RD2"}, ...]}

# 10. 目标要求被批外已认/待复核记录占用则整批拒绝（此时 RD1 由互认 2 占用）
curl -s -X POST localhost:8000/recognitions/reassign -H 'Content-Type: application/json' \
  -d '{"items": [{"recognition_id": 1, "target_requirement_id": "RD1"}]}'
# -> 409, detail 说明 RD1 已被批外互认 2 占用；原互认与进度保持不变
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/courses` | 登记外校课程 |
| PUT | `/courses/{id}` | 成绩/学分更正（不合格互认转待复核） |
| GET | `/courses` | 课程列表 |
| POST | `/requirements` | 登记本校要求 |
| GET | `/requirements` | 要求列表 |
| POST | `/substitutions` | 登记替代规则与最低成绩 |
| GET | `/substitutions` | 规则列表 |
| POST | `/recognitions` | 批量互认（整批确认或整批不写） |
| GET | `/recognitions?status=confirmed\|pending` | 按状态查询互认 |
| POST | `/recognitions/reassign` | 整批改派已认互认到新要求（整批成功或整批不变） |
| POST | `/recognitions/{id}/review` | 复核待复核记录 |
| DELETE | `/recognitions/{id}` | 撤销并释放占用 |
| GET | `/progress` | 进度：已认/待复核/空缺与学分统计 |
