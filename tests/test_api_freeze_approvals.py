"""冻结前比较、审批路由、签署、失效与发布的 API 测试。"""

from __future__ import annotations

import threading

import pytest

from app import services
from app.compliance.freeze_approvals import (
    ANOMALY_HOURS_DECREASED,
    ANOMALY_NEGATIVE_ADJUSTMENT,
    ANOMALY_PENDING_HOURS_INCREASE,
    ANOMALY_REQUIREMENT_REGRESSION,
    ANOMALY_STUDENT_REMOVED,
    ChangePolicy,
    DomainError,
    Role,
    assess_changes,
    approval_identifier,
    is_fully_signed,
    required_roles,
    role_covers,
    route_requirements,
    Signature,
    signature_useful,
    snapshot_fingerprint,
)
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


def _create_plan(client, plan=SHANGHAI_PLAN, **overrides):
    body = dict(plan)
    body.update(overrides)
    resp = client.post("/api/plans", json=body)
    assert resp.status_code == 201, resp.text
    return body


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


def _correction(eid, student, seconds, reason="adjustment"):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _short_checkin(eid="E-02", student="S1"):
    """30 分钟普通签到：只影响学时，不触碰敏感字段。"""
    return _checkin(
        eid,
        student,
        "2024-03-16T08:00:00+08:00",
        "2024-03-16T08:30:00+08:00",
    )


def _post_events(client, pv, events):
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text


def _compare(client, pv, policy=None):
    body = {"policy": policy} if policy is not None else {}
    resp = client.post(f"/api/plans/{pv}/freeze-approvals/comparisons", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _sign(client, pv, approval_id, actor, role, note=""):
    return client.post(
        f"/api/plans/{pv}/freeze-approvals/{approval_id}/signatures",
        json={"actor_id": actor, "actor_role": role, "note": note},
    )


def _publish(client, pv, approval_id, freeze_id=None):
    body = {"freeze_id": freeze_id} if freeze_id else {}
    return client.post(
        f"/api/plans/{pv}/freeze-approvals/{approval_id}/publish", json=body
    )


def _get_approval(client, pv, approval_id):
    return client.get(f"/api/plans/{pv}/freeze-approvals/{approval_id}")


def _seed_one_student(client, pv, seconds=7200):
    """一名学生一次签到，返回后冻结 F-01 作为基线。"""
    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")],
    )
    resp = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# 比较与路由
# ---------------------------------------------------------------------------


def test_compare_pins_fingerprints_and_is_idempotent(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])

    first = _compare(client, pv)
    assert first["created"] is True
    approval = first["approval"]
    assert approval["state"] == "pending"
    assert approval["revision"] == 1
    assert approval["baseline_freeze_id"] == "F-01"
    assert approval["candidate_event_cutoff_id"] == "E-02"
    assert len(approval["baseline_fingerprint"]) == 64
    assert len(approval["candidate_fingerprint"]) == 64
    assert approval["baseline_fingerprint"] != approval["candidate_fingerprint"]
    assert approval["required_roles"] == ["instructor"]
    assert approval["metrics"]["students_modified"] == 1
    assert approval["metrics"]["total_seconds_delta"] == 1800
    assert approval["diff"]["students_affected"] == 1

    # 内容未变时重算幂等：同一审批标识，不产生新记录。
    again = _compare(client, pv)
    assert again["created"] is False
    assert again["approval"]["approval_id"] == approval["approval_id"]
    assert again["approval"]["revision"] == 1

    listing = client.get(f"/api/plans/{pv}/freeze-approvals").json()
    assert len(listing) == 1


def test_compare_without_prior_freeze_uses_empty_baseline(client):
    pv = _create_plan(client)["plan_version"]
    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00")],
    )
    approval = _compare(client, pv)["approval"]
    assert approval["baseline_freeze_id"] is None
    assert approval["metrics"]["students_before"] == 0
    assert approval["metrics"]["students_added"] == 1
    assert approval["metrics"]["students_affected"] == 1


def test_route_endpoint_reports_outstanding_roles(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    approval = _compare(client, pv)["approval"]

    route = client.get(
        f"/api/plans/{pv}/freeze-approvals/{approval['approval_id']}/route"
    ).json()
    assert route["required_roles"] == ["instructor"]
    assert route["satisfied_roles"] == []
    assert route["outstanding_roles"] == ["instructor"]
    assert route["route_reasons"][0]["rule"] == "base"


# ---------------------------------------------------------------------------
# 签署与发布
# ---------------------------------------------------------------------------


def test_full_flow_sign_then_publish_freezes_candidate(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    approval = _compare(client, pv)["approval"]
    aid = approval["approval_id"]

    signed = _sign(client, pv, aid, "teacher-1", "instructor", "ok")
    assert signed.status_code == 201, signed.text
    assert signed.json()["state"] == "approved"
    assert signed.json()["outstanding_roles"] == []

    published = _publish(client, pv, aid, freeze_id="F-02")
    assert published.status_code == 201, published.text
    body = published.json()
    assert body["approval"]["state"] == "published"
    assert body["approval"]["published_freeze_id"] == "F-02"
    assert body["freeze"]["freeze_id"] == "F-02"
    assert body["freeze"]["event_cutoff_id"] == "E-02"
    assert body["freeze"]["students"][0]["total_seconds"] == 7200 + 1800

    frozen = client.get(f"/api/plans/{pv}/freezes/F-02").json()
    assert frozen["students"][0]["total_seconds"] == 9000

    # 重复发布幂等，仍返回首次冻结。
    again = _publish(client, pv, aid, freeze_id="F-OTHER")
    assert again.status_code == 201
    assert again.json()["freeze"]["freeze_id"] == "F-02"

    # 已发布审批不可再签署或失效。
    assert _sign(client, pv, aid, "teacher-2", "dean").status_code == 409
    resp = client.post(
        f"/api/plans/{pv}/freeze-approvals/{aid}/invalidate",
        json={"actor_id": "admin", "reason": "too late"},
    )
    assert resp.status_code == 409


def test_publish_requires_completed_signatures(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_correction("E-02", "S1", 3600)])
    approval = _compare(client, pv)["approval"]

    resp = _publish(client, pv, approval["approval_id"])
    assert resp.status_code == 409


def test_publish_with_existing_freeze_id_conflicts_then_recovers(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    approval = _compare(client, pv)["approval"]
    aid = approval["approval_id"]
    assert _sign(client, pv, aid, "teacher-1", "instructor").status_code == 201

    conflict = _publish(client, pv, aid, freeze_id="F-01")
    assert conflict.status_code == 409
    # 冲突不破坏审批，可换标识重新发布。
    assert _get_approval(client, pv, aid).json()["state"] == "approved"
    ok = _publish(client, pv, aid, freeze_id="F-02")
    assert ok.status_code == 201


def test_publish_defaults_freeze_id_from_approval(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    approval = _compare(client, pv)["approval"]
    aid = approval["approval_id"]
    _sign(client, pv, aid, "teacher-1", "instructor")
    published = _publish(client, pv, aid)
    assert published.status_code == 201
    assert published.json()["freeze"]["freeze_id"] == f"F-{aid}"


# ---------------------------------------------------------------------------
# 权限升级
# ---------------------------------------------------------------------------


def test_higher_role_signature_covers_lower_requirement(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    approval = _compare(client, pv)["approval"]
    assert approval["required_roles"] == ["instructor"]

    # 院长签署向下覆盖导师要求，直接签署完成。
    signed = _sign(client, pv, approval["approval_id"], "dean-1", "dean")
    assert signed.status_code == 201
    assert signed.json()["state"] == "approved"


def test_multi_role_route_partial_coverage_and_escalation(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    # 提高培养方案要求会改变敏感字段 required_seconds，
    # 同时使 S1 的 meets_requirement 翻转（敏感字段）。
    _create_plan(client, required_seconds=7200)
    approval = _compare(client, pv)["approval"]
    aid = approval["approval_id"]
    assert approval["required_roles"] == ["instructor", "dean", "registrar"]
    assert set(approval["metrics"]["sensitive_fields"]) == {
        "meets_requirement",
        "required_seconds",
    }

    # 系主任只能覆盖导师要求。
    r1 = _sign(client, pv, aid, "head-1", "department_head")
    assert r1.status_code == 201
    assert r1.json()["state"] == "pending"
    assert r1.json()["satisfied_roles"] == ["instructor"]
    assert r1.json()["outstanding_roles"] == ["dean", "registrar"]

    # 院长覆盖 dean 要求后仍缺 registrar。
    r2 = _sign(client, pv, aid, "dean-1", "dean")
    assert r2.status_code == 201
    assert r2.json()["outstanding_roles"] == ["registrar"]

    # 再低级别签署无法覆盖剩余要求：403。
    assert _sign(client, pv, aid, "teacher-9", "instructor").status_code == 403
    assert _sign(client, pv, aid, "dean-2", "dean").status_code == 403

    # 教务签署完成审批。
    r3 = _sign(client, pv, aid, "registrar-1", "registrar")
    assert r3.status_code == 201
    assert r3.json()["state"] == "approved"


def test_requirement_regression_routes_to_dean(client):
    pv = _create_plan(client)["plan_version"]
    # 3 小时签到满足 10800 秒要求后冻结。
    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00")],
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    # 负向修正使总学时跌破要求：达标状态回退。
    _post_events(client, pv, [_correction("E-02", "S1", -3600, "leave")])

    approval = _compare(client, pv)["approval"]
    assert "dean" in approval["required_roles"]
    assert "department_head" in approval["required_roles"]
    assert ANOMALY_REQUIREMENT_REGRESSION in approval["metrics"]["anomaly_types"]
    assert ANOMALY_HOURS_DECREASED in approval["metrics"]["anomaly_types"]
    assert ANOMALY_NEGATIVE_ADJUSTMENT in approval["metrics"]["anomaly_types"]
    assert "meets_requirement" in approval["metrics"]["sensitive_fields"]
    rules = {r["rule"] for r in approval["route_reasons"]}
    assert f"anomaly:{ANOMALY_REQUIREMENT_REGRESSION}" in rules
    assert "sensitive:meets_requirement" in rules

    # 导师签署无法满足院长级要求之外的部分，但最终可逐级签完。
    aid = approval["approval_id"]
    assert _sign(client, pv, aid, "t1", "instructor").status_code == 201
    assert _sign(client, pv, aid, "h1", "department_head").status_code == 201
    done = _sign(client, pv, aid, "d1", "dean")
    assert done.status_code == 201
    assert done.json()["state"] == "approved"


# ---------------------------------------------------------------------------
# 阈值边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("student_count", "expected_roles"),
    [
        (5, ["instructor"]),
        (6, ["instructor", "department_head"]),
        (50, ["instructor", "department_head"]),
        (51, ["instructor", "department_head", "dean"]),
    ],
)
def test_student_count_threshold_boundaries(client, student_count, expected_roles):
    pv = _create_plan(client, plan_version=f"P-BOUND-{student_count}")["plan_version"]
    events = [
        _checkin(
            f"E-{i:03d}",
            f"S{i}",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        )
        for i in range(student_count)
    ]
    _post_events(client, pv, events)
    approval = _compare(client, pv)["approval"]
    assert approval["metrics"]["students_affected"] == student_count
    # required_roles 按权限级别升序返回。
    assert approval["required_roles"] == expected_roles
    count_reasons = [
        r for r in approval["route_reasons"] if r["rule"] == "student_count"
    ]
    if student_count <= 5:
        assert count_reasons == []
    else:
        assert count_reasons[0]["role"] in expected_roles


@pytest.mark.parametrize(
    ("delta_seconds", "expected_roles"),
    [
        (36000, ["instructor"]),
        (36001, ["instructor", "department_head"]),
        (360000, ["instructor", "department_head"]),
        (360001, ["instructor", "dean"]),
    ],
)
def test_total_seconds_threshold_boundaries(client, delta_seconds, expected_roles):
    pv = _create_plan(client, plan_version=f"P-SEC-{delta_seconds}")["plan_version"]
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-01T00:00:00+08:00",
                _plus_seconds("2024-03-01T00:00:00+08:00", delta_seconds),
            )
        ],
    )
    approval = _compare(client, pv)["approval"]
    assert approval["metrics"]["total_seconds_delta"] == delta_seconds
    assert approval["required_roles"] == expected_roles


def _plus_seconds(iso: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    base = datetime.fromisoformat(iso)
    return (base + timedelta(seconds=seconds)).isoformat()


def test_custom_policy_overrides_thresholds(client):
    pv = _create_plan(client, plan_version="P-CUSTOM")["plan_version"]
    events = [
        _checkin(
            f"E-{i}",
            f"S{i}",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        )
        for i in range(3)
    ]
    _post_events(client, pv, events)
    policy = {
        "student_moderate_threshold": 1,
        "student_major_threshold": 2,
        "seconds_moderate_threshold": 10_000_000,
        "seconds_major_threshold": 20_000_000,
    }
    approval = _compare(client, pv, policy=policy)["approval"]
    # 3 人超过自定义重大阈值 2：升级到院长（中级要求被更高级覆盖）。
    assert approval["required_roles"] == ["instructor", "dean"]
    assert approval["policy"]["student_major_threshold"] == 2
    assert approval["policy"]["seconds_moderate_threshold"] == 10_000_000
    # 未覆盖的字段保留默认值。
    assert approval["policy"]["base_role"] == "instructor"


def test_invalid_policy_rejected(client):
    pv = _create_plan(client, plan_version="P-BADPOL")["plan_version"]
    resp = client.post(
        f"/api/plans/{pv}/freeze-approvals/comparisons",
        json={"policy": {"base_role": "superuser"}},
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/api/plans/{pv}/freeze-approvals/comparisons",
        json={"policy": {"unknown_field": 1}},
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/api/plans/{pv}/freeze-approvals/comparisons",
        json={"policy": {"student_moderate_threshold": 10, "student_major_threshold": 5}},
    )
    assert resp.status_code == 422


def test_unknown_signer_role_rejected(client):
    pv = _create_plan(client, plan_version="P-BADROLE")["plan_version"]
    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00")],
    )
    approval = _compare(client, pv)["approval"]
    resp = _sign(client, pv, approval["approval_id"], "root", "superuser")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 失效
# ---------------------------------------------------------------------------


def test_candidate_change_invalidates_approval(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_correction("E-02", "S1", 3600)])
    approval = _compare(client, pv)["approval"]
    aid = approval["approval_id"]

    # 候选快照在任何新事件后变化：审批自动失效。
    _post_events(client, pv, [_correction("E-03", "S1", 60)])
    fetched = _get_approval(client, pv, aid).json()
    assert fetched["state"] == "invalidated"
    assert fetched["invalidation_reason"] == "candidate_changed"

    assert _sign(client, pv, aid, "t1", "instructor").status_code == 409
    assert _publish(client, pv, aid).status_code == 409


def test_baseline_change_invalidates_approval(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_correction("E-02", "S1", 3600)])
    approval = _compare(client, pv)["approval"]
    aid = approval["approval_id"]

    # 另一条路径产生了更新的冻结：基线漂移，审批失效。
    client.post(f"/api/plans/{pv}/freezes/F-MANUAL", json={})
    fetched = _get_approval(client, pv, aid).json()
    assert fetched["state"] == "invalidated"
    assert fetched["invalidation_reason"] == "baseline_changed"


def test_manual_invalidate_blocks_and_is_idempotent(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_correction("E-02", "S1", 3600)])
    approval = _compare(client, pv)["approval"]
    aid = approval["approval_id"]

    resp = client.post(
        f"/api/plans/{pv}/freeze-approvals/{aid}/invalidate",
        json={"actor_id": "admin", "reason": "数据存疑，暂缓冻结"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "invalidated"
    assert resp.json()["invalidation_reason"] == "manual"

    again = client.post(
        f"/api/plans/{pv}/freeze-approvals/{aid}/invalidate",
        json={"actor_id": "admin", "reason": "重复操作"},
    )
    assert again.status_code == 200
    assert again.json()["invalidation_reason"] == "manual"

    # 人工否决不能被相同内容的重算绕过。
    recomputed = _compare(client, pv)
    assert recomputed["created"] is False
    assert recomputed["approval"]["state"] == "invalidated"
    assert recomputed["approval"]["revision"] == 1


def test_new_comparison_supersedes_pending_and_revision_reopens(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_correction("E-02", "S1", 3600)])
    first = _compare(client, pv)["approval"]

    _post_events(client, pv, [_correction("E-03", "S1", 1800)])
    second = _compare(client, pv)["approval"]
    assert second["approval_id"] != first["approval_id"]
    stale = _get_approval(client, pv, first["approval_id"]).json()
    assert stale["state"] == "invalidated"
    assert stale["invalidation_reason"] == "superseded"

    # 计划配置变化使候选指纹漂移：审批失效。
    _create_plan(client, required_seconds=999999)
    drifted = _get_approval(client, pv, second["approval_id"]).json()
    assert drifted["state"] == "invalidated"
    assert drifted["invalidation_reason"] == "candidate_changed"

    # 配置恢复后内容回到同一对指纹：以新修订版本重新发起。
    _create_plan(client, required_seconds=SHANGHAI_PLAN["required_seconds"])
    third = _compare(client, pv)
    assert third["created"] is True
    assert third["approval"]["revision"] == 2
    assert third["approval"]["approval_id"] == second["approval_id"] + "-r2"
    assert third["approval"]["state"] == "pending"
    assert (
        third["approval"]["candidate_fingerprint"]
        == second["candidate_fingerprint"]
    )


def test_approval_scoped_to_plan_and_not_found(client):
    pv = _create_plan(client, plan_version="P-SCOPE-A")["plan_version"]
    other = _create_plan(client, plan_version="P-SCOPE-B")["plan_version"]
    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00")],
    )
    approval = _compare(client, pv)["approval"]

    assert _get_approval(client, other, approval["approval_id"]).status_code == 404
    assert _get_approval(client, pv, "FA-missing").status_code == 404
    assert (
        client.post(
            f"/api/plans/{other}/freeze-approvals/{approval['approval_id']}/signatures",
            json={"actor_id": "t", "actor_role": "instructor"},
        ).status_code
        == 404
    )
    resp = client.post("/api/plans/P-MISSING/freeze-approvals/comparisons", json={})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 并发重算与并发签署
# ---------------------------------------------------------------------------


def _run_in_threads(fn, count):
    results: list = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _wrap(i):
        try:
            results.append(fn(i))
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_wrap, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_concurrent_recompute_creates_single_approval(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_correction("E-02", "S1", 3600)])

    def _recompute(_i):
        session = TestSessionLocal()
        try:
            view, created = services.create_comparison(session, plan_version=pv)
            return view["approval_id"], created
        finally:
            session.close()

    results = _run_in_threads(_recompute, 6)
    created_flags = [created for _, created in results]
    assert sum(1 for c in created_flags if c) == 1
    assert sum(1 for c in created_flags if not c) == 5
    assert len({aid for aid, _ in results}) == 1

    listing = client.get(f"/api/plans/{pv}/freeze-approvals").json()
    assert len(listing) == 1
    assert listing[0]["state"] == "pending"


def test_concurrent_duplicate_signature_is_idempotent(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    aid = _compare(client, pv)["approval"]["approval_id"]

    def _sign_same_actor(_i):
        session = TestSessionLocal()
        try:
            return services.sign_approval(
                session,
                plan_version=pv,
                approval_id=aid,
                actor_id="teacher-1",
                actor_role="instructor",
            )
        finally:
            session.close()

    results = _run_in_threads(_sign_same_actor, 4)
    # 每个并发调用都看到恰好一条签名；状态视图可能先于
    # 胜出事务的“批准”提交返回，但最终状态必然一致。
    assert all(len(view["signatures"]) == 1 for view in results)

    final = _get_approval(client, pv, aid).json()
    assert final["state"] == "approved"
    assert len(final["signatures"]) == 1


# ---------------------------------------------------------------------------
# 重启恢复
# ---------------------------------------------------------------------------


def test_restart_recovers_approval_and_completes_publish(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    aid = _compare(client, pv)["approval"]["approval_id"]
    assert _sign(client, pv, aid, "teacher-1", "instructor").status_code == 201

    # 模拟服务重启：全新会话从数据库恢复审批状态。
    session = TestSessionLocal()
    try:
        route = services.get_route(session, pv, aid)
        assert route["state"] == "approved"
        assert route["outstanding_roles"] == []
        assert [s["actor_id"] for s in route["signatures"]] == ["teacher-1"]

        view, freeze = services.publish_approval(
            session, plan_version=pv, approval_id=aid, freeze_id="F-02"
        )
        assert view["state"] == "published"
        assert freeze["students"][0]["total_seconds"] == 9000
    finally:
        session.close()

    assert client.get(f"/api/plans/{pv}/freezes/F-02").status_code == 200


def test_restart_detects_candidate_drift_and_blocks_publish(client):
    pv = _create_plan(client)["plan_version"]
    _seed_one_student(client, pv)
    _post_events(client, pv, [_short_checkin()])
    aid = _compare(client, pv)["approval"]["approval_id"]
    assert _sign(client, pv, aid, "teacher-1", "instructor").status_code == 201

    # “停机”期间候选发生变化。
    _post_events(client, pv, [_correction("E-03", "S1", 120)])

    # 重启后的新会话立即检测到指纹漂移并拒绝发布。
    session = TestSessionLocal()
    try:
        with pytest.raises(services.ApprovalConflictError):
            services.publish_approval(
                session, plan_version=pv, approval_id=aid, freeze_id="F-02"
            )
        view = services.get_approval_view(session, pv, aid)
        assert view["state"] == "invalidated"
        assert view["invalidation_reason"] == "candidate_changed"
    finally:
        session.close()

    assert client.get(f"/api/plans/{pv}/freezes/F-02").status_code == 404


# ---------------------------------------------------------------------------
# 领域逻辑单元测试
# ---------------------------------------------------------------------------


def _student(student_id, **fields):
    base = {
        "student_id": student_id,
        "confirmed_seconds": 0,
        "pending_seconds": 0,
        "adjustment_seconds": 0,
        "total_seconds": 0,
        "lesson_units": 0,
        "pending_lesson_units": 0,
        "meets_requirement": False,
    }
    base.update(fields)
    return base


def _snapshot(students, required_seconds=10800, timezone="Asia/Shanghai"):
    return {
        "plan_version": "P-UNIT",
        "freeze_id": None,
        "timezone": timezone,
        "required_seconds": required_seconds,
        "generated_at": "2024-03-15T00:00:00Z",
        "event_cutoff_id": None,
        "students": students,
    }


def test_fingerprint_ignores_volatile_metadata():
    snap = _snapshot([_student("S1", total_seconds=10)])
    other = dict(snap)
    other["generated_at"] = "2025-01-01T00:00:00Z"
    other["freeze_id"] = "F-99"
    assert snapshot_fingerprint(snap) == snapshot_fingerprint(other)

    changed = _snapshot([_student("S1", total_seconds=11)])
    assert snapshot_fingerprint(snap) != snapshot_fingerprint(changed)


def test_assess_changes_detects_anomaly_types():
    baseline = _snapshot(
        [
            _student("S-REMOVED", total_seconds=100),
            _student("S-REGRESS", total_seconds=10800, meets_requirement=True),
            _student("S-ADJ", adjustment_seconds=0, total_seconds=500),
            _student("S-PENDING", pending_seconds=100),
        ]
    )
    candidate = _snapshot(
        [
            _student("S-REGRESS", total_seconds=9000, meets_requirement=False),
            _student("S-ADJ", adjustment_seconds=-200, total_seconds=300),
            _student("S-PENDING", pending_seconds=500),
            _student("S-NEW", total_seconds=60),
        ]
    )
    metrics = assess_changes(baseline, candidate)
    assert metrics.students_removed == 1
    assert metrics.students_added == 1
    assert metrics.students_modified == 3
    assert metrics.students_affected == 5
    # 基线总学时 100+10800+500+0，候选 9000+300+0+60（S-PENDING 总学时为 0）。
    assert metrics.total_seconds_before == 11400
    assert metrics.total_seconds_after == 9360
    assert metrics.total_seconds_delta == 2040
    assert set(metrics.anomaly_types) == {
        ANOMALY_STUDENT_REMOVED,
        ANOMALY_REQUIREMENT_REGRESSION,
        ANOMALY_HOURS_DECREASED,
        ANOMALY_NEGATIVE_ADJUSTMENT,
        ANOMALY_PENDING_HOURS_INCREASE,
    }
    assert "adjustment_seconds" in metrics.sensitive_fields
    assert "meets_requirement" in metrics.sensitive_fields


def test_assess_changes_flags_plan_level_sensitive_fields():
    baseline = _snapshot([_student("S1", total_seconds=10)])
    candidate = _snapshot([_student("S1", total_seconds=10)], required_seconds=7200)
    metrics = assess_changes(baseline, candidate)
    assert "required_seconds" in metrics.sensitive_fields
    assert metrics.students_affected == 0


def test_route_thresholds_are_strictly_greater():
    policy = ChangePolicy()
    metrics = assess_changes(
        _snapshot([]),
        _snapshot([_student(f"S{i}") for i in range(5)]),
    )
    roles = required_roles(route_requirements(metrics, policy))
    # 恰好 5 人不升级（阈值语义为严格大于）。
    assert roles == ("instructor",)

    metrics = assess_changes(
        _snapshot([]),
        _snapshot([_student(f"S{i}") for i in range(6)]),
    )
    roles = required_roles(route_requirements(metrics, policy))
    assert "department_head" in roles
    assert "dean" not in roles


def test_role_coverage_and_signature_usefulness():
    assert role_covers(Role.DEAN, Role.INSTRUCTOR)
    assert not role_covers(Role.INSTRUCTOR, Role.DEAN)

    required = ["instructor", "dean"]
    signed = [Signature(actor_id="a", actor_role="dean")]
    assert is_fully_signed(required, signed)

    pending = [Signature(actor_id="a", actor_role="instructor")]
    assert not is_fully_signed(required, pending)
    # 导师再签无法覆盖剩余 dean 要求。
    assert not signature_useful(
        required, pending, Signature(actor_id="b", actor_role="instructor")
    )
    assert signature_useful(
        required, pending, Signature(actor_id="b", actor_role="dean")
    )


def test_policy_validation_and_merge():
    with pytest.raises(DomainError):
        ChangePolicy.from_dict({"base_role": "superuser"})
    with pytest.raises(DomainError):
        ChangePolicy.from_dict({"nonsense": 1})
    with pytest.raises(DomainError):
        ChangePolicy.from_dict(
            {"student_moderate_threshold": 10, "student_major_threshold": 5}
        )

    policy = ChangePolicy.from_dict(
        {"student_moderate_threshold": 1, "student_major_threshold": 3}
    )
    assert policy.student_major_threshold == 3
    assert policy.student_moderate_threshold == 1
    # 未覆盖字段保留默认。
    assert policy.seconds_moderate_threshold == 36000
    assert policy.anomaly_type_roles[ANOMALY_STUDENT_REMOVED] == "department_head"


def test_approval_identifier_is_deterministic():
    a = approval_identifier("P1", "b" * 64, "c" * 64)
    b = approval_identifier("P1", "b" * 64, "c" * 64)
    c = approval_identifier("P1", "b" * 64, "d" * 64)
    assert a == b
    assert a != c
    assert a.startswith("FA-")
