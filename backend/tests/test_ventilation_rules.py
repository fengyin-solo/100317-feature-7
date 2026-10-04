"""通风判定规则端到端验证：直接跑 .venv/bin/python backend/tests/test_ventilation_rules.py。"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.ventilation_rules import (  # noqa: E402
    STATUS_DERATED,
    STATUS_FAULT,
    STATUS_NORMAL,
    classify_by_ratio,
    stricter,
    validate_thresholds,
)

client = TestClient(app)
passed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    assert condition, f"✗ {name} {detail}"
    passed += 1
    print(f"✓ {name}")


# ---------- 1. 规则单元：边界不重叠、取严只一份 ----------

thresholds = {"故障阈值": 0.3, "降频阈值": 0.5}
check("比例 0.29 判故障停机", classify_by_ratio(0.29, thresholds) == STATUS_FAULT)
check("比例恰好 0.30 落降频档（边界不重叠）", classify_by_ratio(0.30, thresholds) == STATUS_DERATED)
check("比例 0.49 落降频档", classify_by_ratio(0.49, thresholds) == STATUS_DERATED)
check("比例恰好 0.50 判正常", classify_by_ratio(0.50, thresholds) == STATUS_NORMAL)
check("取严：降频与正常 -> 降频", stricter(STATUS_DERATED, STATUS_NORMAL) == STATUS_DERATED)
check("取严：故障与降频 -> 故障", stricter(STATUS_FAULT, STATUS_DERATED) == STATUS_FAULT)
check("阈值边界重叠被拒", validate_thresholds(0.5, 0.5) is not None)
check("故障阈值高于降频被拒", validate_thresholds(0.6, 0.5) is not None)
check("合法阈值通过", validate_thresholds(0.3, 0.5) is None)


# ---------- 2. 设备建档：没有额定风量不许保存 ----------

def create(values: dict) -> dict:
    return client.post("/api/ventilation", json={"values": values}).json()


res = create({"设备编号": "VENT-T1", "设备类型": "主通风机"})
check("缺额定风量被拒", res["ok"] is False and "额定风量" in res["message"], res["message"])

res = create({"设备编号": "VENT-T2", "设备类型": "主通风机", "额定风量": 0})
check("额定风量为 0 被拒", res["ok"] is False)

res = create({"设备编号": "VENT-T3", "设备类型": "主通风机", "额定风量": -100})
check("额定风量为负被拒", res["ok"] is False)

res = create({"设备编号": "VENT-T4", "设备类型": "主通风机", "额定风量": "abc"})
check("额定风量非数字被拒", res["ok"] is False)

res = create({"设备编号": "VENT-T5", "设备类型": "主通风机", "额定风量": 1000, "频率下限": 55})
check("频率范围超出 30~50 口径被拒", res["ok"] is False, res.get("message", ""))

res = create({"设备编号": "VENT-T6", "设备类型": "主通风机", "额定风量": 1000, "频率下限": 45, "频率上限": 35})
check("下限大于上限被拒", res["ok"] is False)

res = create({"设备编号": "VENT-OK", "设备类型": "主通风机", "额定风量": 1000, "运行时段": [["08:00", "16:00"]]})
check("合法设备建档成功", res["ok"] is True, res.get("message", ""))
fan = res["entry"]
check("默认频率范围 30~50", fan["频率下限"] == 30.0 and fan["频率上限"] == 50.0)


def action(fan_id: int, act: str, **extra) -> tuple[int, dict]:
    response = client.post(f"/api/ventilation/{fan_id}/actions", json={"values": {"action": act, **extra}})
    return response.status_code, response.json()


# ---------- 3. 实测判定入口（五成/三成） ----------

def report(fan_id: int, airflow: float) -> tuple[int, dict]:
    response = client.post(f"/api/ventilation/{fan_id}/airflow", json={"measured_airflow": airflow})
    return response.status_code, response.json()


status, res = report(fan["id"], 400)  # 40% -> 降频
check("实测 40% 自动判降频", status == 200 and res["entry"]["status"] == STATUS_DERATED, res.get("detail", ""))
check("设备进入异常有起始时刻", bool(res["entry"]["异常起于"]))

# ---------- 4. 并发两次降频，只生效一次，后到原样拒绝 ----------

fan2 = create({"设备编号": "VENT-CC", "设备类型": "局扇", "额定风量": 500})["entry"]
results: list[tuple[int, str]] = []
barrier = threading.Barrier(2)


def submit_derate() -> None:
    barrier.wait()
    code, payload = action(fan2["id"], "降频运行")
    message = payload.get("entry", {}).get("status") if code == 200 else payload.get("detail", "")
    results.append((code, message))


threads = [threading.Thread(target=submit_derate) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ok_count = sum(1 for code, _ in results if code == 200)
reject = next(((code, msg) for code, msg in results if code != 200), None)
check("并发两次降频仅一次生效", ok_count == 1, str(results))
check("后到的降频原样 409 拒绝", reject is not None and reject[0] == 409 and "重复降频" in reject[1], str(reject))

# 恢复后再降频应正常（锁与计数不残留）
code, res = action(fan2["id"], "恢复正常")
check("恢复正常成功", code == 200 and res["entry"]["status"] == STATUS_NORMAL, str(res))
code, res = action(fan2["id"], "降频运行")
check("恢复后重新降频可生效", code == 200 and res["entry"]["status"] == STATUS_DERATED, str(res))


# ---------- 5. 判定打架取严（两个人工/实测入口共用一份规则） ----------

fan3 = create({"设备编号": "VENT-ST", "设备类型": "局扇", "额定风量": 500})["entry"]
# 人工点"降频"，但同单带实测 100/500=20% -> 应取严为故障
code, res = action(fan3["id"], "降频运行", measured_airflow=100)
check("人工降频与实测故障打架 -> 取严判故障", code == 200 and res["entry"]["status"] == STATUS_FAULT, str(res))

# 故障状态点降频 -> 拒绝回退
code, res = action(fan3["id"], "降频运行")
check("故障态请求降频被 409 拒绝", code == 409, res.get("detail", ""))

# 纯实测入口：25% 对已是降频的设备不允许回退（取严保持）
code, res = report(fan3["id"], 125)  # 25%
check("故障态实测仍判故障不回退", code == 200 and res["entry"]["status"] == STATUS_FAULT)


# ---------- 6. 欠风时长按运行时段算，回写台账 ----------

from app.services import ventilation as vent_module  # noqa: E402

base = datetime(2026, 10, 1, 0, 0)
clock_value = base


def fake_clock() -> datetime:
    return clock_value


vent_service = vent_module.VentilationService(clock=fake_clock)
# 阈值已由真实 service 种过，共享同一 store
dev, errs = vent_service.create_entry(
    {"设备编号": "VENT-TIME", "设备类型": "局扇", "额定风量": 400, "运行时段": [["08:00", "16:00"]]}
)
check("运行时段设备建档", not errs, str(errs))

# 07:30 进入降频，09:30 恢复：运行时段内欠风 90 分钟（07:30-08:00 不计）
clock_value = datetime(2026, 10, 1, 7, 30)
vent_service.judge_by_airflow(dev["id"], 150)  # 37.5%
clock_value = datetime(2026, 10, 1, 9, 30)
vent_service.run_action(dev["id"], "恢复正常")
ledger = vent_service.list_ledger(device_id=dev["id"])
check("运行时段外的半小时不计欠风", len(ledger) == 1 and ledger[0]["欠风分钟数"] == 90.0, str(ledger))

# 跨天异常 + 归档：09:00 故障，次日 10:30 恢复；先归档 10-01
dev2, _ = vent_service.create_entry(
    {"设备编号": "VENT-CROSS", "设备类型": "主扇", "额定风量": 800, "运行时段": [["08:00", "18:00"]]}
)
clock_value = datetime(2026, 10, 1, 9, 0)
vent_service.judge_by_airflow(dev2["id"], 100)
clock_value = datetime(2026, 10, 2, 0, 0)
vent_service.close_day(base.date())
row_day1 = next(r for r in vent_service.list_ledger(device_id=dev2["id"]) if r["日期"] == "2026-10-01")
check("归档日欠风只算 09:00-18:00 共 540 分钟", row_day1["欠风分钟数"] == 540.0, str(row_day1))
check("归档后记录冻结标记为真", row_day1["已归档"] is True)
check("归档记录钉死阈值版本号", row_day1["阈值版本号"] == 1)

# ---------- 7. 阈值调整：新版本生效，归档日不重算，旧记录按当时阈值还原 ----------

# 阈值在注入时钟的时间线上调整（10-02 零点起新版本生效）
version, msg = vent_service.adjust_thresholds(0.2, 0.4, "新规程加严")
check("阈值调整成功", version is not None and version["版本号"] == 2, msg)
versions = client.get("/api/ventilation/thresholds").json()
check("当前为第 2 版阈值", versions["current"]["版本号"] == 2)
check("旧版本仍保留可回溯", len(versions["versions"]) == 2)

# 归档的 10-01 欠风分钟数原封不动
after = vent_service.list_ledger(device_id=dev2["id"])
row_day1_after = next(r for r in after if r["日期"] == "2026-10-01")
check("阈值调整后已归档欠风时长不重算", row_day1_after["欠风分钟数"] == 540.0 and row_day1_after["阈值版本号"] == 1)

# 还原旧记录：按 v1 口径；用 v2 对照只给对照不改写
code = client.get(f"/api/ventilation/ledger/{row_day1_after['id']}/replay")
check("旧台账按当时阈值还原", code.status_code == 200 and "第 1 版" in code.json()["message"])
code = client.get(f"/api/ventilation/ledger/{row_day1_after['id']}/replay?version_id={versions['versions'][-1]['id']}")
body = code.json()
check("对照新版本不覆盖统计", body["entry"]["欠风分钟数"] == 540.0 and "对照版本" in body["entry"])

# 新判定按新阈值：35% 在旧版(0.3/0.5)是降频，新版(0.2/0.4)仍是降频；取 45% 验证 -> 新版正常
clock_value = datetime(2026, 10, 2, 10, 30)
dev3, _ = vent_service.create_entry({"设备编号": "VENT-NEW", "设备类型": "局扇", "额定风量": 400})
clock_value = datetime(2026, 10, 2, 10, 31)
_, msg, status_code = vent_service.judge_by_airflow(dev3["id"], 180)  # 45%
entry_now = vent_service.get_entry(dev3["id"])
check("阈值调整后新判定按新口径（45% 新版算正常）", entry_now["status"] == STATUS_NORMAL, msg)

# 非法阈值（重叠）拒绝
bad = client.put("/api/ventilation/thresholds", json={"fault_ratio": 0.5, "derate_ratio": 0.5})
check("重叠阈值不允许保存", bad.json()["ok"] is False and "重叠" in bad.json()["message"])


# ---------- 8. 运行日志：状态变化全留痕 ----------

logs = client.get(f"/api/ventilation/logs?device_id={fan3['id']}").json()["items"]
actions_seen = {row["动作"] for row in logs}
check("实测上报写进运行日志", "实测风量上报" in actions_seen, str(actions_seen))
check("日志记录前后状态", all("前状态" in row and "后状态" in row for row in logs))
threshold_logs = client.get("/api/ventilation/logs").json()["items"]
check("阈值调整写进运行日志", any(row["动作"] == "阈值调整" for row in threshold_logs))
check("台账归档写进运行日志", any(row["动作"] == "台账归档" for row in threshold_logs))


# ---------- 9. 无额定风量：列表中的种子设备额定风量均为正数 ----------

listing = client.get("/api/ventilation?size=200").json()["items"]
check("列表设备额定风量均为正数", all(float(r["额定风量"]) > 0 for r in listing))


print(f"\n全部 {passed} 项断言通过")
