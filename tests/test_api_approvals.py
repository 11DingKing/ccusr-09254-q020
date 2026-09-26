"""冻结前比较审批：比较、路由、签署、失效与发布的 API 与服务测试。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import approvals
from app.core.change_policy import ChangePolicy, Role, evaluate_policy
from app.db import get_db
from app.main import app
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(eid, student, seconds, reason=""):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _four_hour_checkin(eid, student):
    """4 小时签到，满足 SHANGHAI_PLAN 的 10800 秒要求。"""
    return _checkin(
        eid, student, "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
    )


def _one_hour_checkin(eid, student):
    return _checkin(
        eid, student, "2024-03-16T13:00:00+08:00", "2024-03-16T14:00:00+08:00"
    )


def _post_events(client, plan_version, events):
    resp = client.post(f"/api/plans/{plan_version}/events", json={"events": events})
    assert resp.status_code == 201, resp.text


def _sign(client, pv, cid, role, signer):
    return client.post(
        f"/api/plans/{pv}/comparisons/{cid}/signatures",
        json={"role": role, "signer_id": signer},
    )


def test_compare_route_sign_publish_happy_path(client):
    """小变化只需教务员签署，签署齐全后发布为正式冻结。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_one_hour_checkin("E-02", "S1")])

    resp = client.post(f"/api/plans/{pv}/comparisons")
    assert resp.status_code == 201, resp.text
    cmp = resp.json()
    cid = cmp["comparison_id"]
    assert cmp["status"] == "pending"
    assert cmp["route"] == ["officer"]
    assert cmp["base_freeze_id"] == "F-01"
    assert cmp["candidate_cutoff_id"] == "E-02"
    assert cmp["stale"] is False
    summary = cmp["summary"]
    assert summary["students_affected"] == 1
    assert summary["total_seconds_delta"] == 3600
    assert summary["anomalies"] == []
    assert summary["sensitive_changes"] == []

    # 重复比较幂等，收敛到同一条记录。
    again = client.post(f"/api/plans/{pv}/comparisons").json()
    assert again["comparison_id"] == cid

    route = client.get(f"/api/plans/{pv}/comparisons/{cid}/route").json()
    assert route["required_roles"] == ["officer"]
    assert route["next_role"] == "officer"
    assert route["complete"] is False

    resp = _sign(client, pv, cid, "officer", "officer-1")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "approved"

    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/publish", json={"freeze_id": "F-02"}
    )
    assert pub.status_code == 201, pub.text
    assert pub.json()["freeze_id"] == "F-02"
    assert pub.json()["event_cutoff_id"] == "E-02"
    assert pub.json()["students"][0]["total_seconds"] == 18000

    view = client.get(f"/api/plans/{pv}/comparisons/{cid}").json()
    assert view["status"] == "published"
    assert view["published_freeze_id"] == "F-02"

    frozen = client.get(f"/api/plans/{pv}/freezes/F-02").json()
    assert frozen["students"][0]["total_seconds"] == 18000


def test_first_freeze_compares_against_empty_base(client):
    """没有历史冻结时与空基准比较，全部学生记为新增。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    assert cmp["base_freeze_id"] is None
    assert cmp["summary"]["students_added"] == 1
    assert cmp["route"] == ["officer"]

    cid = cmp["comparison_id"]
    assert _sign(client, pv, cid, "officer", "officer-1").status_code == 201
    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/publish", json={"freeze_id": "F-01"}
    )
    assert pub.status_code == 201, pub.text


def test_no_changes_approves_immediately(client):
    """候选与上一次冻结一致时无需签署，可直接发布。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    assert cmp["route"] == []
    assert cmp["status"] == "approved"
    assert cmp["summary"]["students_affected"] == 0

    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cmp['comparison_id']}/publish",
        json={"freeze_id": "F-02"},
    )
    assert pub.status_code == 201, pub.text


def test_permission_escalation_requires_director(client):
    """受影响学生数达到阈值，签署链升级到系主任。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    students = [f"S{i}" for i in range(1, 6)]
    _post_events(
        client, pv, [_four_hour_checkin(f"E-0{i}", s) for i, s in enumerate(students, 1)]
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(
        client, pv, [_one_hour_checkin(f"E-1{i}", s) for i, s in enumerate(students, 1)]
    )

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    cid = cmp["comparison_id"]
    assert cmp["route"] == ["officer", "director"]
    assert cmp["summary"]["students_affected"] == 5

    # 教务员签署后仍需系主任；越级或重复签署都被拒绝。
    assert _sign(client, pv, cid, "officer", "officer-1").status_code == 201
    route = client.get(f"/api/plans/{pv}/comparisons/{cid}/route").json()
    assert route["next_role"] == "director"
    assert route["complete"] is False

    assert _sign(client, pv, cid, "officer", "officer-2").status_code == 409
    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/publish", json={"freeze_id": "F-02"}
    )
    assert pub.status_code == 409

    assert _sign(client, pv, cid, "director", "director-1").status_code == 201
    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/publish", json={"freeze_id": "F-02"}
    )
    assert pub.status_code == 201, pub.text


def test_sensitive_change_escalates_to_dean(client):
    """达标状态翻转等敏感字段变化需要院长签署。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_correction("E-02", "S1", -7200, "revoked leave")])

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    cid = cmp["comparison_id"]
    assert cmp["route"] == ["officer", "director", "dean"]
    assert set(cmp["summary"]["sensitive_changes"]) == {
        "adjustment_seconds",
        "meets_requirement",
    }
    assert "negative_adjustment" in cmp["summary"]["anomalies"]
    assert "requirement_not_met" in cmp["summary"]["anomalies"]

    # 必须按链顺序签署：院长不能先签。
    assert _sign(client, pv, cid, "dean", "dean-1").status_code == 409
    assert _sign(client, pv, cid, "officer", "officer-1").status_code == 201
    assert _sign(client, pv, cid, "dean", "dean-1").status_code == 409
    assert _sign(client, pv, cid, "director", "director-1").status_code == 201
    resp = _sign(client, pv, cid, "dean", "dean-1")
    assert resp.status_code == 201
    assert resp.json()["status"] == "approved"

    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/publish", json={"freeze_id": "F-02"}
    )
    assert pub.status_code == 201, pub.text
    assert pub.json()["students"][0]["total_seconds"] == 7200


def test_candidate_change_invalidates_approval(client):
    """签署后候选快照再变化，发布即失败且审批失效。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_one_hour_checkin("E-02", "S1")])

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    cid = cmp["comparison_id"]
    assert _sign(client, pv, cid, "officer", "officer-1").status_code == 201

    # 新事件改变了候选快照。
    _post_events(client, pv, [_one_hour_checkin("E-03", "S1")])

    view = client.get(f"/api/plans/{pv}/comparisons/{cid}").json()
    assert view["stale"] is True

    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/publish", json={"freeze_id": "F-02"}
    )
    assert pub.status_code == 409
    view = client.get(f"/api/plans/{pv}/comparisons/{cid}").json()
    assert view["status"] == "invalid"
    assert "fingerprint" in view["invalidated_reason"]

    # 重新比较生成新记录，走完流程后可正常发布。
    fresh = client.post(f"/api/plans/{pv}/comparisons").json()
    assert fresh["comparison_id"] != cid
    assert fresh["status"] == "pending"
    assert _sign(client, pv, fresh["comparison_id"], "officer", "officer-1").status_code == 201
    pub = client.post(
        f"/api/plans/{pv}/comparisons/{fresh['comparison_id']}/publish",
        json={"freeze_id": "F-02"},
    )
    assert pub.status_code == 201, pub.text


def test_sign_after_candidate_change_fails(client):
    """签署过程中候选变化，后续签署被拒绝且审批失效。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_correction("E-02", "S1", -7200, "revoked")])

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    cid = cmp["comparison_id"]
    assert cmp["route"] == ["officer", "director", "dean"]
    assert _sign(client, pv, cid, "officer", "officer-1").status_code == 201

    _post_events(client, pv, [_correction("E-03", "S1", 600, "partial restore")])

    resp = _sign(client, pv, cid, "director", "director-1")
    assert resp.status_code == 409
    view = client.get(f"/api/plans/{pv}/comparisons/{cid}").json()
    assert view["status"] == "invalid"


def test_publish_invalidates_other_pending_comparisons(client):
    """发布推进基准冻结后，其余待审批比较全部失效。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_one_hour_checkin("E-02", "S1")])
    cmp1 = client.post(f"/api/plans/{pv}/comparisons").json()
    _post_events(client, pv, [_one_hour_checkin("E-03", "S1")])
    cmp2 = client.post(f"/api/plans/{pv}/comparisons").json()
    assert cmp1["comparison_id"] != cmp2["comparison_id"]

    # cmp1 的候选已被 E-03 取代；签署并发布较新的 cmp2。
    cid2 = cmp2["comparison_id"]
    assert _sign(client, pv, cid2, "officer", "officer-1").status_code == 201
    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid2}/publish", json={"freeze_id": "F-02"}
    )
    assert pub.status_code == 201, pub.text

    view1 = client.get(
        f"/api/plans/{pv}/comparisons/{cmp1['comparison_id']}"
    ).json()
    assert view1["status"] == "invalid"
    assert "F-02" in view1["invalidated_reason"]


def test_explicit_invalidate_blocks_sign_and_publish(client):
    """显式作废后不能再签署或发布。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_one_hour_checkin("E-02", "S1")])

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    cid = cmp["comparison_id"]
    resp = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/invalidate",
        json={"reason": "教务处撤回本次冻结"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "invalid"
    assert resp.json()["invalidated_reason"] == "教务处撤回本次冻结"

    assert _sign(client, pv, cid, "officer", "officer-1").status_code == 409
    pub = client.post(
        f"/api/plans/{pv}/comparisons/{cid}/publish", json={"freeze_id": "F-02"}
    )
    assert pub.status_code == 409


def test_concurrent_compare_only_one_created(client):
    """并发重算同一候选，只有一方创建比较记录。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_one_hour_checkin("E-02", "S1")])

    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def _compare():
        session = TestSessionLocal()
        try:
            view, created = approvals.compare_candidate(session, plan_version=pv)
            with lock:
                results.append((view["comparison_id"], created))
        finally:
            session.close()

    threads = [threading.Thread(target=_compare) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for _, created in results if created) == 1
    assert len({cid for cid, _ in results}) == 1


def test_restart_recovery_mid_approval(client):
    """审批中途重启（全新会话与客户端）后状态完整恢复。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _post_events(client, pv, [_correction("E-02", "S1", -7200, "revoked")])

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    cid = cmp["comparison_id"]
    assert cmp["route"] == ["officer", "director", "dean"]
    assert _sign(client, pv, cid, "officer", "officer-1").status_code == 201

    # 模拟服务重启：丢弃现有会话，使用全新会话与客户端。
    session2 = TestSessionLocal()

    def override2():
        yield session2

    app.dependency_overrides[get_db] = override2
    try:
        with TestClient(app) as client2:
            view = client2.get(f"/api/plans/{pv}/comparisons/{cid}").json()
            assert view["status"] == "pending"
            assert [s["role"] for s in view["signatures"]] == ["officer"]
            assert view["next_role"] == "director"

            assert _sign(client2, pv, cid, "director", "director-1").status_code == 201
            resp = _sign(client2, pv, cid, "dean", "dean-1")
            assert resp.status_code == 201
            assert resp.json()["status"] == "approved"

            pub = client2.post(
                f"/api/plans/{pv}/comparisons/{cid}/publish",
                json={"freeze_id": "F-02"},
            )
            assert pub.status_code == 201, pub.text
            assert pub.json()["students"][0]["total_seconds"] == 7200
    finally:
        session2.close()


def test_unknown_comparison_and_role_errors(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, pv, [_four_hour_checkin("E-01", "S1")])

    assert client.get(f"/api/plans/{pv}/comparisons/nope").status_code == 404
    assert _sign(client, pv, "nope", "officer", "u1").status_code == 404
    assert (
        client.post(
            f"/api/plans/{pv}/comparisons/nope/publish", json={"freeze_id": "F-1"}
        ).status_code
        == 404
    )
    assert client.post("/api/plans/NOPE/comparisons").status_code == 404

    cmp = client.post(f"/api/plans/{pv}/comparisons").json()
    cid = cmp["comparison_id"]
    # 非法角色无法通过请求校验。
    assert (
        client.post(
            f"/api/plans/{pv}/comparisons/{cid}/signatures",
            json={"role": "president", "signer_id": "u1"},
        ).status_code
        == 422
    )


def test_custom_policy_thresholds_via_service(client):
    """服务层可注入策略：受影响学生数阈值决定是否升级。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    strict_plan = dict(SHANGHAI_PLAN, plan_version="P-SH-STRICT")
    _create_plan(client, plan=strict_plan)
    events = [
        _four_hour_checkin("E-01", "S1"),
        _four_hour_checkin("E-02", "S2"),
    ]
    for plan_version in (pv, strict_plan["plan_version"]):
        _post_events(client, plan_version, events)
        client.post(f"/api/plans/{plan_version}/freezes/F-01", json={})
        _post_events(
            client,
            plan_version,
            [_one_hour_checkin("E-03", "S1"), _one_hour_checkin("E-04", "S2")],
        )

    session = TestSessionLocal()
    try:
        default_view, _ = approvals.compare_candidate(session, plan_version=pv)
        assert default_view["route"] == ["officer"]

        strict = ChangePolicy(student_count_threshold=2)
        strict_view, _ = approvals.compare_candidate(
            session, plan_version=strict_plan["plan_version"], policy=strict
        )
        assert strict_view["route"] == ["officer", "director"]
    finally:
        session.close()


_CLEAN_CANDIDATE = [
    {"meets_requirement": True, "pending_seconds": 0, "adjustment_seconds": 0}
]


def _modified(sid, fields):
    return {"student_id": sid, "change_type": "modified", "fields": fields}


def _seconds_change(before, after):
    return {"total_seconds": {"before": before, "after": after}}


@pytest.mark.parametrize(
    "changes,candidate,expected",
    [
        # 空差异：无需签署。
        ([], _CLEAN_CANDIDATE, ()),
        # 低于阈值的普通修改：仅教务员。
        (
            [_modified("S1", _seconds_change(100, 200))],
            _CLEAN_CANDIDATE,
            (Role.OFFICER,),
        ),
        # 学生数恰好在阈值之下。
        (
            [
                _modified("S1", _seconds_change(0, 100)),
                _modified("S2", _seconds_change(0, 100)),
            ],
            _CLEAN_CANDIDATE,
            (Role.OFFICER,),
        ),
        # 学生数恰好等于阈值：升级到系主任。
        (
            [
                _modified("S1", _seconds_change(0, 100)),
                _modified("S2", _seconds_change(0, 100)),
                _modified("S3", _seconds_change(0, 100)),
            ],
            _CLEAN_CANDIDATE,
            (Role.OFFICER, Role.DIRECTOR),
        ),
        # 总学时波动恰好在阈值之下/等于阈值。
        (
            [_modified("S1", _seconds_change(0, 3599))],
            _CLEAN_CANDIDATE,
            (Role.OFFICER,),
        ),
        (
            [_modified("S1", _seconds_change(0, 3600))],
            _CLEAN_CANDIDATE,
            (Role.OFFICER, Role.DIRECTOR),
        ),
        # 敏感字段变化：直接升级到院长。
        (
            [
                _modified(
                    "S1",
                    {"meets_requirement": {"before": True, "after": False}},
                )
            ],
            _CLEAN_CANDIDATE,
            (Role.OFFICER, Role.DIRECTOR, Role.DEAN),
        ),
        # 异常类型：未达标、待确认、负向修正、学生移除均升级到院长。
        (
            [_modified("S1", _seconds_change(0, 100))],
            [{"meets_requirement": False, "pending_seconds": 0, "adjustment_seconds": 0}],
            (Role.OFFICER, Role.DIRECTOR, Role.DEAN),
        ),
        (
            [_modified("S1", _seconds_change(0, 100))],
            [{"meets_requirement": True, "pending_seconds": 10, "adjustment_seconds": 0}],
            (Role.OFFICER, Role.DIRECTOR, Role.DEAN),
        ),
        (
            [_modified("S1", _seconds_change(0, 100))],
            [{"meets_requirement": True, "pending_seconds": 0, "adjustment_seconds": -1}],
            (Role.OFFICER, Role.DIRECTOR, Role.DEAN),
        ),
        (
            [
                {
                    "student_id": "S9",
                    "change_type": "removed",
                    "before": {"total_seconds": 100},
                }
            ],
            _CLEAN_CANDIDATE,
            (Role.OFFICER, Role.DIRECTOR, Role.DEAN),
        ),
    ],
)
def test_policy_threshold_boundaries(changes, candidate, expected):
    """阈值边界：等于阈值即升级，敏感字段与异常直达院长。"""
    policy = ChangePolicy(student_count_threshold=3, total_seconds_threshold=3600)
    evaluation = evaluate_policy(
        {"student_changes": changes}, candidate, policy
    )
    assert evaluation.required_roles == expected
