#!/usr/bin/env python3
"""盘后兜底自检：判断"该有当日盘后产物了，但仓库里没有"。

纯本地、无网络、无写盘 —— 可以对着任意 checkout 直接跑：
    WATCHDOG_DATE=2026-11-20 WATCHDOG_GLOBS='reports/{D}.md' python3 scripts/postclose_watchdog.py

env:
  WATCHDOG_GLOBS   必填。逗号分隔的相对 glob，{D} 替换为 YYYY-MM-DD；任一命中即视为"已产出"。
  WATCHDOG_ROOT    仓库根目录，默认当前目录。
  WATCHDOG_DATE    覆盖待检查日期（测试/手工回溯用）。
  WATCHDOG_AFTER   盘后判定门限，北京时间 HH:MM，默认 16:45。
  WATCHDOG_OPEN    开盘前恢复窗上沿，北京时间 HH:MM，默认 09:25。

stdout 一行 JSON；退出码 0=已产出/无需补跑，10=缺失需补跑，20=不在判定窗口，2=配置错误。
"""
import datetime as dt
import glob as globmod
import json
import os
import sys

try:
    from zoneinfo import ZoneInfo
    SH = ZoneInfo("Asia/Shanghai")
except Exception:
    SH = dt.timezone(dt.timedelta(hours=8))


def hhmm(text, default):
    try:
        h, m = str(text).split(":")
        return dt.time(int(h), int(m))
    except Exception:
        return default


def target_day(now, after, open_before):
    """返回 (date, mode)；mode is None 表示当前时刻不该判定。"""
    t = now.time()
    if t >= after:
        return now.date(), "postclose"
    if t <= open_before:
        d = now.date() - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return (d, "preopen") if d.weekday() < 5 else (None, None)
    return None, None


def main():
    root = os.environ.get("WATCHDOG_ROOT", ".")
    pats = [p.strip() for p in os.environ.get("WATCHDOG_GLOBS", "").split(",") if p.strip()]
    if not pats:
        print(json.dumps({"error": "WATCHDOG_GLOBS is empty"}, ensure_ascii=False))
        return 2

    now = dt.datetime.now(SH)
    override = os.environ.get("WATCHDOG_DATE")
    if override:
        day, mode = dt.date.fromisoformat(override), "manual"
    else:
        day, mode = target_day(now,
                              hhmm(os.environ.get("WATCHDOG_AFTER"), dt.time(16, 45)),
                              hhmm(os.environ.get("WATCHDOG_OPEN"), dt.time(9, 25)))
        if mode is None:
            print(json.dumps({"date": None, "missing": False, "note": "not-in-window"}, ensure_ascii=False))
            return 20
    if day.weekday() >= 5:
        print(json.dumps({"date": day.isoformat(), "missing": False, "note": "weekend"}, ensure_ascii=False))
        return 20

    d = day.isoformat()
    matched, tried = [], []
    for pat in pats:
        rel = pat.replace("{D}", d)
        tried.append(rel)
        for h in sorted(globmod.glob(os.path.join(root, rel))):
            matched.append(os.path.relpath(h, root))

    print(json.dumps({"date": d, "mode": mode, "missing": not matched,
                     "matched": matched, "looked_for": tried,
                     "beijing_now": now.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
    return 10 if not matched else 0


if __name__ == "__main__":
    sys.exit(main())
