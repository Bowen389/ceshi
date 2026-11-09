"""Post-close scan and independent paper ledgers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import market_data

REPORTS = Path(__file__).resolve().parent / "reports"
STRATEGIES = {"FB_DUAL_K4": (4, 4, 0), "U4": (4, 4, 3), "V6": (4, 2, 3)}
STARTING_CASH = 200_000
MAIN_BOARD = r"(?:600|601|603|605|000|001|002|003)\d{3}"


def index_prices():
    url = "https://quotes.sina.cn/cn/api/jsonp.php/var%20x=/CN_MarketData.getKLineData"
    response = requests.get(url, params={"symbol": "sh000852", "scale": 240, "ma": "no", "datalen": 150}, timeout=25)
    response.raise_for_status()
    body = response.text
    data = json.loads(body[body.index("([") + 1:body.rindex("])") + 1])
    return pd.Series({r["day"]: float(r["close"]) for r in data}).sort_index()


def load_panel(days):
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(market_data.package, days))
    frames = []
    for day in days:
        frame = pd.read_csv(market_data.ROOT / f"{day}.csv", dtype={"code": str})
        frame = frame[frame.code.str.fullmatch(MAIN_BOARD, na=False)]
        if len(frame) < 2500 or set(frame.date) != {day}:
            raise ValueError(f"{day} 主板行情覆盖异常")
        frames.append(frame)
    panel = market_data.prepare(pd.concat(frames, ignore_index=True))
    panel["eligible"] = ~panel.name.str.contains(r"ST|退|^[NC]", case=False, na=True)
    rate = np.where(panel.name.str.contains("ST", case=False, na=False), 5, 10)
    cents = np.rint(panel.prev_close * 100).astype("int64")
    panel["upper"] = ((cents * (100 + rate) + 50) // 100) / 100
    panel["lower"] = ((cents * (100 - rate) + 50) // 100) / 100
    panel["traded"] = panel.volume.gt(0)
    panel["sealed"] = panel.traded & (panel.close - panel.upper).abs().lt(.005)
    panel["touched"] = panel.traded & (panel.high - panel.upper).abs().lt(.005) & ~panel.sealed
    return panel


def scan(panel, days, csi):
    today = days[-1]
    out = {k: [] for k in STRATEGIES}
    index = csi.reindex(days[-20:])
    if index.isna().any() or not np.isfinite(index).all() or (index <= 0).any():
        raise ValueError(f"{today} 中证1000 缺少有效 MA20 数据")
    fb_gate = bool(index.iloc[-1] < index.mean())
    active = panel[panel.traded]
    previous_days = pd.DataFrame({"date": days[1:], "prior": days[:-1]})
    previous_close = active[["date", "code", "close"]].rename(columns={"date": "prior", "close": "prior_close"})
    pairs = active[["date", "code", "close"]].merge(previous_days).merge(previous_close, on=["prior", "code"])
    market_return = ((pairs.close / pairs.prior_close - 1).clip(-.11, .11)
                     .groupby(pairs.date).mean().reindex(days[1:]))
    if market_return.isna().any():
        raise ValueError("主板指数有缺失交易日")
    market = (1 + market_return).cumprod()
    deep_market = bool(market.iloc[-1] / market.tail(20).mean() - 1 < -.05
                       or market.iloc[-1] / market.iloc[-21] - 1 < -.10)
    candidates = panel[(panel.date == today) & panel.sealed & panel.eligible]
    pool = panel[panel.code.isin(candidates.code)]
    for code, group in pool.groupby("code", sort=False):
        h = group.set_index("date").reindex(days)
        t, prev = h.iloc[-1], h.iloc[-2]
        if prev.sealed == True:
            continue
        traded = group[group.traded]
        item = {"code": code, "name": t["name"]}
        position_window = h.adj_close.iloc[-61:-1]
        if (fb_gate and t.amount >= 1e8 and t.open < t.upper - .005
                and pd.notna(h.close.iloc[-120]) and position_window.notna().all()):
            pos = float(position_window.iloc[-1] / position_window.median())
            if pos < 1:
                out["FB_DUAL_K4"].append(dict(item, arm="C2" if pos < .7 else "C1", pos=pos,
                                             amount=float(t.amount)))
        if len(traded) < 60 or t.close < 3 or prev.traded != True:
            continue
        window = h.amount.iloc[-21:-1].where(h.traded.iloc[-21:-1].eq(True))
        if window.isna().any():
            continue
        mean = float(window.mean())
        engulf = t.adj_close > max(prev.adj_open, prev.adj_close)
        one_board = h.sealed.iloc[-7:-2].eq(True).sum() == 1
        down = prev.close < prev.open
        a = engulf and one_board and (down or prev.touched == True) and mean >= 2.5e8
        peak = h.close.iloc[-122:-2]
        b = (h.sealed.iloc[-11:-2].eq(True).any() and down and mean >= 1e8
             and peak.notna().all() and prev.close < .95 * peak.max())
        if a or b:
            out["U4"].append(dict(item, arm="U4a+U4b" if a and b else "U4a" if a else "U4b", mean=mean))
        a = one_board and mean >= 2.5e8 and t.open >= t.prev_close
        deep = mean >= 3e7 and (deep_market or t.adj_close / traded.adj_close.tail(60).max() - 1 < -.30)
        if engulf and (a or deep):
            out["V6"].append(dict(item, arm="双臂" if a and deep else "A类" if a else "深跌", mean=mean))
    for key, rows in out.items():
        rows.sort(key=lambda row: (-row.get("mean", 0), row["code"]))
    return out


def fresh():
    return {key: {"cash": STARTING_CASH, "positions": {}, "peak": STARTING_CASH,
                  "paused": False, "nav": [], "trades": []} for key in STRATEGIES}


def fee(key, gross, selling):
    commission = .00025 if key == "FB_DUAL_K4" else .0001
    tax = .0001 if key == "V6" else .0005
    transfer = 0 if key == "FB_DUAL_K4" else .00001
    return max(5, gross * commission) + gross * (transfer + (tax if selling else 0))


def settle(book, previous, today, bars):
    for key, (slots, daily, extensions) in STRATEGIES.items():
        account = book[key]
        held = account["positions"]
        cash = account["cash"]
        equity = cash + sum(p["value"] for p in held.values())
        queue = []
        for row in previous[key]:
            quote = bars.get(row["code"])
            if not quote or not quote["eligible"] or not quote["traded"] or not 0 < quote["open"] < quote["upper"] - .005:
                continue
            gap = round(quote["open"] / quote["prev_close"] - 1, 10)
            if key == "FB_DUAL_K4":
                lo, hi = (-.025, .05) if row["arm"] == "C2" else (-.05, -.02)
                if not lo <= gap <= hi:
                    continue
                rank = (0 if row["arm"] == "C2" else 1, gap, row["code"])
            else:
                rank = (0, -row["mean"], row["code"])
            queue.append((rank, row, quote))
        bought = 0
        if not account["paused"]:
            for _, row, quote in sorted(queue, key=lambda x: x[0]):
                code = row["code"]
                if bought >= daily or len(held) >= slots:
                    break
                if code in held:
                    continue
                price = min(quote["upper"], quote["open"] * (1 if key == "FB_DUAL_K4" else 1.0005))
                cap = min(equity / 4 * .99, cash * .99, row["amount"] * .001) if key == "FB_DUAL_K4" else min(50_000, cash)
                shares = int(cap / price / 100) * 100
                while shares and shares * price + fee(key, shares * price, False) > cash:
                    shares -= 100
                if not shares:
                    continue
                cost = shares * price
                buy_fee = fee(key, cost, False)
                cash -= cost + buy_fee
                held[code] = dict(row, buy_date=today, buy_price=price, shares=shares,
                                  buy_fee=buy_fee, cost=cost, value=cost, extensions=0)
                bought += 1
        for code, position in list(held.items()):
            quote = bars.get(code)
            if not quote or not quote["traded"]:
                continue
            new = position["buy_date"] == today
            reference = position["buy_price"] if new else quote["prev_close"]
            position["value"] *= quote["close"] / reference
            if new or quote["close"] <= quote["lower"] + .005:
                continue
            if quote["sealed"] and position["extensions"] < extensions:
                position["extensions"] += 1
                continue
            price = max(quote["lower"], quote["close"] * (1 if key == "FB_DUAL_K4" else .9995))
            gross = position["value"] * price / quote["close"]
            sell_fee = fee(key, gross, True)
            cash += gross - sell_fee
            account["trades"].append(dict(position, sell_date=today, sell_price=price,
                                          sell_fee=sell_fee, net=gross - sell_fee - position["cost"] - position["buy_fee"]))
            del held[code]
        equity = cash + sum(p["value"] for p in held.values())
        account["cash"] = cash
        account["peak"] = max(account["peak"], equity)
        if key == "FB_DUAL_K4" and equity <= .65 * account["peak"]:
            account["paused"] = True
        account["nav"].append({"date": today, "equity": equity, "cash": cash, "positions": len(held)})


def publish(day, plans, book):
    REPORTS.mkdir(exist_ok=True)
    payload = {"date": day, "candidates": plans, "book": book}
    target = REPORTS / "state.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf8")
    lines = [f"# {day} 盘后预选", "", "三个独立模拟账户，每个起始资金 20 万元。候选需在下一交易日开盘确认。", "",
             "| 策略 | 账户净值 | 可用现金 | 持仓 | 累计平仓 | 候选数 |", "|---|---:|---:|---:|---:|---:|"]
    for key, rows in plans.items():
        account = book[key]
        eq = account["cash"] + sum(p["value"] for p in account["positions"].values())
        lines.append(f"| {key} | {eq:,.2f} | {account['cash']:,.2f} | {len(account['positions'])} | {len(account['trades'])} | {len(rows)} |")
    for key, rows in plans.items():
        lines += ["", f"## {key}", ""]
        if book[key]["paused"]:
            lines.append("暂停新开仓，持仓继续按规则退出。")
        for row in rows:
            value = f"位置 {row['pos']:.3f}" if "pos" in row else f"20日均额 {row['mean']/1e8:.2f} 亿"
            lines.append(f"- {row['code']} {row['name']} · {row['arm']} · {value}")
        if not rows:
            lines.append("本次无合格候选。")
    report = "\n".join(lines) + "\n"
    for filename in (f"{day}.md", "latest.md"):
        (REPORTS / filename).write_text(report, encoding="utf8")
    temp.replace(target)


def main():
    from exchange_calendars import get_calendar
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="显式指定已收盘交易日")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    date = args.date or now.date().isoformat()
    cal = get_calendar("XSHG")
    if not cal.is_session(date) or (date == now.date().isoformat() and now.time() < time(15)):
        print("当前日期没有已收盘交易日")
        return
    if date > now.date().isoformat():
        raise ValueError("不能扫描未来日期")
    state_file = REPORTS / "state.json"
    state = json.loads(state_file.read_text(encoding="utf8")) if state_file.exists() else None
    if state and state["date"] >= date:
        print(f"已处理到 {state['date']}")
        return
    all_days = [d.date().isoformat() for d in cal.sessions if d.date().isoformat() <= date]
    first = all_days.index(state["date"]) + 1 if state else len(all_days) - 1
    start = max(0, first - 149)
    days = all_days[start:]
    csi = index_prices()
    panel = load_panel(days)
    book = state["book"] if state else fresh()
    previous = state["candidates"] if state else None
    for i in range(first - start, len(days)):
        day = days[i]
        history = days[max(0, i - 149):i + 1]
        plans = scan(panel[panel.date.isin(history)], history, csi)
        if previous is not None:
            quotes = panel[panel.date == day].set_index("code").to_dict("index")
            settle(book, previous, day, quotes)
        publish(day, plans, book)
        previous = plans


if __name__ == "__main__":
    main()
