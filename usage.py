"""What each user's requests to DeepSeek cost, recorded on this machine.

Every model call and web search lands in one ledger with who made it and which agent asked, so spend can be
shown per user, per day and per agent before more agents start calling the model on their own.
Prices are yuan per million tokens at peak; off-peak is half (DeepSeek pricing page, 2026-11).
"""
import contextlib
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3

log = logging.getLogger("uvicorn.error")
BEIJING = timezone(timedelta(hours=8))  # No daylight saving, so a fixed offset is exact.
# Peak: weekdays 9:00-12:00 and 14:00-18:00 Beijing time. Public holidays are off-peak too, but no
# holiday calendar is kept here, so a holiday weekday is priced at peak: spend is overstated, never understated.
PEAK_HOURS = ((9, 12), (14, 18))
PRICES = {
    "deepseek-flash": {"hit": 0.04, "miss": 2.0, "output": 8.0},
    "deepseek-v4-flash": {"hit": 0.04, "miss": 2.0, "output": 8.0},
    "deepseek-v4-pro": {"hit": 0.30, "miss": 9.0, "output": 27.0},
}
SEARCH_YUAN = 0.005  # 5 yuan per 1,000 successful searches.
SCHEMA = """CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL,
    kind TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL, hit_tokens INTEGER NOT NULL DEFAULT 0,
    miss_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    peak INTEGER NOT NULL DEFAULT 0, cost_yuan REAL, complete INTEGER NOT NULL DEFAULT 1)"""


def is_peak(moment):
    moment = moment.astimezone(BEIJING)
    return moment.weekday() < 5 and any(start <= moment.hour < end for start, end in PEAK_HOURS)


def chat_cost(model, hit, miss, output, moment):
    """Yuan for one call, or None for a model with no known price (shown as unpriced, not as free)."""
    price = PRICES.get(model)
    if price is None:
        return None
    factor = 1 if is_peak(moment) else .5
    return (hit * price["hit"] + miss * price["miss"] + output * price["output"]) / 1e6 * factor


class UsageLedger:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(SCHEMA)
        return connection

    def _insert(self, user_id, kind, agent, model, moment, hit=0, miss=0, output=0, cost=None, complete=True):
        moment = (moment or datetime.now(BEIJING)).astimezone(BEIJING)
        # Losing a ledger row must never cost the user their reply.
        try:
            with contextlib.closing(self.connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO usage (user_id, day, created_at, kind, agent, model, hit_tokens, miss_tokens, output_tokens,"
                    " peak, cost_yuan, complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, moment.date().isoformat(), moment.isoformat(timespec="seconds"), kind, agent, model,
                     hit, miss, output, int(is_peak(moment)), cost, int(complete)))
        except sqlite3.Error:
            log.exception("could not write the usage ledger")

    def record_chat(self, user_id, model, usage, agent="main", moment=None):
        """usage is DeepSeek's usage object, or None when the stream was cut off before it arrived.

        A cut-off call is still billed for what it used, but the count never reached us; it is kept as an
        incomplete row so the page can say how many calls are missing instead of quietly reporting less.
        """
        moment = moment or datetime.now(BEIJING)
        if usage is None:
            self._insert(user_id, "chat", agent, model, moment, complete=False)
            return
        hit = usage.get("prompt_cache_hit_tokens") or 0
        miss = usage.get("prompt_cache_miss_tokens")
        if miss is None:
            miss = max(0, (usage.get("prompt_tokens") or 0) - hit)
        output = usage.get("completion_tokens") or 0
        self._insert(user_id, "chat", agent, model, moment, hit, miss, output, chat_cost(model, hit, miss, output, moment))

    def record_search(self, user_id, agent="main", moment=None):
        self._insert(user_id, "search", agent, "web_search", moment, cost=SEARCH_YUAN)

    def summary(self, user_id=None, days=14, today=None):
        """Totals for today, this month and each recent day; by agent; by user when user_id is None (admin)."""
        today = today or datetime.now(BEIJING).date()
        start = (today - timedelta(days=days - 1)).isoformat()
        month = today.replace(day=1).isoformat()
        where, args = ("WHERE user_id = ?", [user_id]) if user_id is not None else ("WHERE 1 = 1", [])
        columns = ("COUNT(*), COALESCE(SUM(cost_yuan), 0), SUM(hit_tokens), SUM(miss_tokens), SUM(output_tokens),"
                   " SUM(kind = 'search'), SUM(complete = 0), SUM(cost_yuan IS NULL AND complete = 1)")

        def totals(row):
            calls, cost, hit, miss, output, searches, incomplete, unpriced = row
            hit, miss = hit or 0, miss or 0
            return {"calls": calls, "yuan": round(cost, 6), "hit_tokens": hit, "miss_tokens": miss,
                    "output_tokens": output or 0, "searches": searches or 0, "incomplete": incomplete or 0,
                    "unpriced": unpriced or 0, "cache_rate": round(hit / (hit + miss), 3) if hit + miss else None}

        with contextlib.closing(self.connect()) as connection:
            def query(extra, extra_args=(), group=""):
                sql = f"SELECT {group + ', ' if group else ''}{columns} FROM usage {where} {extra}"
                return connection.execute(sql + (f" GROUP BY {group} ORDER BY {group}" if group else ""), [*args, *extra_args]).fetchall()

            result = {
                "today": totals(query("AND day = ?", [today.isoformat()])[0]),
                "month": totals(query("AND day >= ?", [month])[0]),
                "days": [{"day": row[0], **totals(row[1:])} for row in query("AND day >= ?", [start], "day")],
                "agents": [{"agent": row[0], **totals(row[1:])} for row in query("AND day >= ?", [month], "agent")],
            }
            if user_id is None:
                result["users"] = [{"user_id": row[0], **totals(row[1:])} for row in query("AND day >= ?", [month], "user_id")]
        return result


LEDGER = UsageLedger(Path(__file__).resolve().parent / "data" / "usage.db")
