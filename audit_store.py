import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "review.db"
REVIEWABLE_STATUSES = {"待领取", "复核中"}


def _connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _database():
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _add_column_if_missing(connection, table, column, definition):
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def initialize_database(seed_tickets=None):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _database() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cases (
            case_id TEXT PRIMARY KEY,
            copy TEXT NOT NULL,
            industry TEXT NOT NULL DEFAULT '护肤品',
            category TEXT NOT NULL,
            decision TEXT NOT NULL,
            risk TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            advertiser TEXT NOT NULL,
            industry TEXT NOT NULL,
            product TEXT NOT NULL,
            copy TEXT NOT NULL,
            risk TEXT NOT NULL,
            priority TEXT NOT NULL,
            status TEXT NOT NULL,
            ai_decision TEXT NOT NULL,
            matched_rules TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            suggested_copy TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            reviewer TEXT NOT NULL DEFAULT '',
            final_decision TEXT NOT NULL DEFAULT '',
            report_id TEXT NOT NULL DEFAULT '',
            placement TEXT NOT NULL DEFAULT '',
            material_type TEXT NOT NULL DEFAULT '',
            review_queue TEXT NOT NULL DEFAULT '',
            sla_deadline TEXT NOT NULL DEFAULT '',
            landing_page_text TEXT NOT NULL DEFAULT '',
            qualification_verified INTEGER NOT NULL DEFAULT 0,
            brand_terms TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            decision TEXT NOT NULL,
            note TEXT NOT NULL,
            original_copy TEXT NOT NULL,
            ai_decision TEXT NOT NULL,
            report_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
        );
        """)
        _add_column_if_missing(connection, "review_logs", "report_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "cases", "industry", "TEXT NOT NULL DEFAULT '护肤品'")
        _add_column_if_missing(connection, "tickets", "placement", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "tickets", "material_type", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "tickets", "review_queue", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "tickets", "sla_deadline", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "tickets", "landing_page_text", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "tickets", "qualification_verified", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(connection, "tickets", "brand_terms", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(connection, "tickets", "audience", "TEXT NOT NULL DEFAULT ''")
        cases = json.loads((ROOT / "data" / "cases.json").read_text(encoding="utf-8"))
        case_payloads = [{"industry": item.get("industry", "护肤品"), **item} for item in cases]
        connection.executemany(
            "INSERT OR REPLACE INTO cases(case_id,copy,industry,category,decision,risk,reason) VALUES(:case_id,:copy,:industry,:category,:decision,:risk,:reason)",
            case_payloads,
        )
        for ticket in seed_tickets or []:
            payload = _ticket_to_db(ticket, now)
            connection.execute(
                """INSERT OR IGNORE INTO tickets(
                    ticket_id,advertiser,industry,product,copy,risk,priority,status,
                    ai_decision,matched_rules,submitted_at,suggested_copy,note,
                    reviewer,final_decision,report_id,placement,material_type,
                    review_queue,sla_deadline,landing_page_text,
                    qualification_verified,brand_terms,audience,updated_at
                ) VALUES(
                    :ticket_id,:advertiser,:industry,:product,:copy,:risk,:priority,:status,
                    :ai_decision,:matched_rules,:submitted_at,:suggested_copy,:note,
                    :reviewer,:final_decision,:report_id,:placement,:material_type,
                    :review_queue,:sla_deadline,:landing_page_text,
                    :qualification_verified,:brand_terms,:audience,:updated_at
                )""",
                payload,
            )
            connection.execute(
                """UPDATE tickets SET
                    placement=CASE WHEN placement='' THEN :placement ELSE placement END,
                    material_type=CASE WHEN material_type='' THEN :material_type ELSE material_type END,
                    review_queue=CASE WHEN review_queue='' THEN :review_queue ELSE review_queue END,
                    sla_deadline=CASE WHEN sla_deadline='' THEN :sla_deadline ELSE sla_deadline END
                    WHERE ticket_id=:ticket_id""",
                payload,
            )


def _ticket_to_db(ticket, updated_at=None):
    return {
        "ticket_id": ticket.get("工单号", ""),
        "advertiser": ticket.get("广告主", ""),
        "industry": ticket.get("行业", "护肤品"),
        "product": ticket.get("商品", ""),
        "copy": ticket.get("文案", ""),
        "risk": ticket.get("风险", "低"),
        "priority": ticket.get("优先级", "P2"),
        "status": ticket.get("状态", "待领取"),
        "ai_decision": ticket.get("机器结论", ""),
        "matched_rules": ticket.get("命中规则", ""),
        "submitted_at": ticket.get("提交时间", updated_at or ""),
        "suggested_copy": ticket.get("建议文案", ""),
        "note": ticket.get("备注", ""),
        "reviewer": ticket.get("审核员", ""),
        "final_decision": ticket.get("最终结论", ""),
        "report_id": ticket.get("报告号", ""),
        "placement": ticket.get("广告位", ""),
        "material_type": ticket.get("素材类型", "图文"),
        "review_queue": ticket.get("审核队列", "美妆普通队列"),
        "sla_deadline": ticket.get("SLA截止", ""),
        "landing_page_text": ticket.get("落地页信息", ""),
        "qualification_verified": int(bool(ticket.get("资质已核验", False))),
        "brand_terms": ticket.get("品牌禁用词", ""),
        "audience": ticket.get("目标人群", ""),
        "updated_at": updated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _ticket_from_db(row):
    return {
        "工单号": row["ticket_id"], "广告主": row["advertiser"],
        "行业": row["industry"], "商品": row["product"], "文案": row["copy"],
        "风险": row["risk"], "优先级": row["priority"], "状态": row["status"],
        "机器结论": row["ai_decision"], "命中规则": row["matched_rules"],
        "提交时间": row["submitted_at"], "建议文案": row["suggested_copy"],
        "备注": row["note"], "审核员": row["reviewer"],
        "最终结论": row["final_decision"], "报告号": row["report_id"],
        "广告位": row["placement"], "素材类型": row["material_type"],
        "审核队列": row["review_queue"], "SLA截止": row["sla_deadline"],
        "落地页信息": row["landing_page_text"],
        "资质已核验": bool(row["qualification_verified"]),
        "品牌禁用词": row["brand_terms"], "目标人群": row["audience"],
        "更新时间": row["updated_at"],
    }


def create_ticket(ticket):
    payload = _ticket_to_db(ticket)
    with _database() as connection:
        connection.execute(
            """INSERT INTO tickets(
                ticket_id,advertiser,industry,product,copy,risk,priority,status,
                ai_decision,matched_rules,submitted_at,suggested_copy,note,
                reviewer,final_decision,report_id,placement,material_type,
                review_queue,sla_deadline,landing_page_text,
                qualification_verified,brand_terms,audience,updated_at
            ) VALUES(
                :ticket_id,:advertiser,:industry,:product,:copy,:risk,:priority,:status,
                :ai_decision,:matched_rules,:submitted_at,:suggested_copy,:note,
                :reviewer,:final_decision,:report_id,:placement,:material_type,
                :review_queue,:sla_deadline,:landing_page_text,
                :qualification_verified,:brand_terms,:audience,:updated_at
            )""",
            payload,
        )
    return ticket["工单号"]


def list_tickets():
    with _database() as connection:
        rows = connection.execute(
            "SELECT * FROM tickets ORDER BY updated_at DESC, submitted_at DESC"
        ).fetchall()
    return [_ticket_from_db(row) for row in rows]


def search_similar_cases(copy, industry=None, limit=3):
    with _database() as connection:
        if industry:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM cases WHERE industry=?", (industry,)
            ).fetchall()]
        else:
            rows = [dict(row) for row in connection.execute("SELECT * FROM cases").fetchall()]
    query_chars = set(copy)
    for row in rows:
        case_chars = set(row["copy"])
        union = query_chars | case_chars
        row["score"] = round(len(query_chars & case_chars) / max(1, len(union)), 3)
    return sorted(rows, key=lambda row: row["score"], reverse=True)[:limit]


def save_human_decision(
    ticket_id, reviewer, decision, note, original_copy, ai_decision, report_id=""
):
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _database() as connection:
        cursor = connection.execute(
            """UPDATE tickets SET status=?, reviewer=?, final_decision=?, note=?,
               report_id=?, updated_at=? WHERE ticket_id=? AND status IN (?,?)""",
            (
                decision, reviewer, decision, note, report_id, created_at,
                ticket_id, *sorted(REVIEWABLE_STATUSES),
            ),
        )
        if cursor.rowcount != 1:
            row = connection.execute(
                "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown ticket id: {ticket_id}")
            raise ValueError(f"Ticket {ticket_id} is not reviewable: {row['status']}")
        connection.execute(
            """INSERT INTO review_logs(
                ticket_id,reviewer,decision,note,original_copy,ai_decision,report_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (ticket_id, reviewer, decision, note, original_copy, ai_decision, report_id, created_at),
        )
    return created_at


def list_review_logs(limit=50, ticket_id=None):
    with _database() as connection:
        if ticket_id:
            rows = connection.execute(
                "SELECT * FROM review_logs WHERE ticket_id=? ORDER BY id DESC LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM review_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(row) for row in rows]
