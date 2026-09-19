"""双库连接与表结构。

身份库 identity.db：家庭、成员、同意、随访、通知、审计，以及
analysis_id -> member_id 的高敏对应表（对应表只存在于身份库）。
分析库 analysis.db：匿名场所、干预版本、设备、校准、读数、人时、
冻结估算与血缘。分析库中永不出现真实姓名、住址或 member_id。
"""

import sqlite3
import threading

from .config import REGION_COUNT

IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    household_id   TEXT PRIMARY KEY,
    community      TEXT NOT NULL,
    region_code    TEXT NOT NULL,
    contact_ref    TEXT NOT NULL,
    enrolled_on    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS household_region_history (
    household_id   TEXT NOT NULL,
    region_code    TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT
);

CREATE TABLE IF NOT EXISTS members (
    member_id      TEXT PRIMARY KEY,
    household_id   TEXT NOT NULL REFERENCES households(household_id),
    pseudonym      TEXT NOT NULL,
    age_band       TEXT NOT NULL,
    role           TEXT NOT NULL,
    enrolled_on    TEXT NOT NULL,
    withdrawn_on   TEXT
);

CREATE TABLE IF NOT EXISTS consents (
    consent_id     TEXT PRIMARY KEY,
    household_id   TEXT NOT NULL REFERENCES households(household_id),
    scope          TEXT NOT NULL,
    version        TEXT NOT NULL,
    granted_on     TEXT NOT NULL,
    revoked_on     TEXT
);

CREATE TABLE IF NOT EXISTS analysis_links (
    analysis_id    TEXT PRIMARY KEY,
    member_id      TEXT NOT NULL UNIQUE REFERENCES members(member_id),
    household_id   TEXT NOT NULL REFERENCES households(household_id),
    created_on     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followups (
    followup_id    TEXT PRIMARY KEY,
    member_id      TEXT NOT NULL REFERENCES members(member_id),
    observed_on    TEXT NOT NULL,
    symptoms       TEXT NOT NULL,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    reason_code     TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    delivered_at    TEXT,
    delivery_note   TEXT
);

CREATE TABLE IF NOT EXISTS notification_targets (
    notification_id TEXT NOT NULL REFERENCES notifications(notification_id),
    analysis_id     TEXT NOT NULL,
    member_id       TEXT NOT NULL REFERENCES members(member_id),
    household_id    TEXT NOT NULL REFERENCES households(household_id),
    PRIMARY KEY (notification_id, member_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    detail       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
    region_code TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    site_id           TEXT PRIMARY KEY,
    region_code       TEXT NOT NULL REFERENCES regions(region_code),
    site_type         TEXT NOT NULL,
    anonymized_label  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interventions (
    intervention_id TEXT PRIMARY KEY,
    region_code     TEXT NOT NULL REFERENCES regions(region_code),
    site_id         TEXT REFERENCES sites(site_id),
    policy_version  TEXT NOT NULL,
    effective_date  TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS devices (
    device_serial TEXT PRIMARY KEY,
    assign_type   TEXT NOT NULL DEFAULT 'site',
    site_id       TEXT REFERENCES sites(site_id),
    analysis_id   TEXT,
    deployed_on   TEXT
);

CREATE TABLE IF NOT EXISTS calibration_sessions (
    session_id     TEXT PRIMARY KEY,
    device_serial  TEXT NOT NULL REFERENCES devices(device_serial),
    calibrated_at  TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT,
    gain           REAL NOT NULL DEFAULT 1.0,
    offset         REAL NOT NULL DEFAULT 0.0,
    status         TEXT NOT NULL DEFAULT 'valid'
);

CREATE TABLE IF NOT EXISTS readings (
    reading_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    device_serial   TEXT NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    pm25            REAL NOT NULL,
    received_at     TEXT NOT NULL,
    ingest_batch    TEXT NOT NULL,
    session_id      TEXT,
    valid_for_estimate INTEGER NOT NULL DEFAULT 0,
    exclude_reason  TEXT,
    UNIQUE (device_serial, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS person_periods (
    analysis_id     TEXT NOT NULL,
    region_code     TEXT NOT NULL,
    intervention_id TEXT,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    person_days     INTEGER NOT NULL,
    consent_id      TEXT NOT NULL,
    consent_version TEXT NOT NULL,
    age_band        TEXT NOT NULL,
    role            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estimate_versions (
    version_id     TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    method_name    TEXT NOT NULL,
    method_params  TEXT NOT NULL,
    weights_json   TEXT NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    region_count   INTEGER NOT NULL,
    frozen         INTEGER NOT NULL DEFAULT 1,
    freeze_hash    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estimate_region_results (
    version_id   TEXT NOT NULL REFERENCES estimate_versions(version_id),
    region_code  TEXT NOT NULL,
    point        REAL,
    ci_low       REAL,
    ci_high      REAL,
    weight       REAL NOT NULL,
    n_readings   INTEGER NOT NULL,
    n_households INTEGER NOT NULL,
    n_person_days INTEGER NOT NULL,
    quality      TEXT NOT NULL,
    PRIMARY KEY (version_id, region_code)
);

CREATE TABLE IF NOT EXISTS estimate_lineage_readings (
    version_id    TEXT NOT NULL,
    region_code   TEXT NOT NULL,
    reading_id    INTEGER NOT NULL,
    device_serial TEXT NOT NULL,
    session_id    TEXT,
    included      INTEGER NOT NULL,
    reason        TEXT
);

CREATE TABLE IF NOT EXISTS estimate_lineage_persons (
    version_id      TEXT NOT NULL,
    region_code     TEXT NOT NULL,
    analysis_id     TEXT NOT NULL,
    age_band        TEXT NOT NULL,
    role            TEXT NOT NULL,
    person_days     INTEGER NOT NULL,
    consent_id      TEXT NOT NULL,
    consent_version TEXT NOT NULL,
    consent_active  INTEGER NOT NULL,
    symptom_alert   INTEGER NOT NULL,
    intervention_id TEXT
);
"""


def _region_codes():
    return [f"R{i:03d}" for i in range(1, REGION_COUNT + 1)]


class Databases:
    """持有身份库与分析库的独立连接，写操作各自加锁。"""

    def __init__(self, identity_path="identity.db", analysis_path="analysis.db"):
        self.identity = sqlite3.connect(
            identity_path, check_same_thread=False, isolation_level=None
        )
        self.analysis = sqlite3.connect(
            analysis_path, check_same_thread=False, isolation_level=None
        )
        for conn in (self.identity, self.analysis):
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
        self.ilock = threading.RLock()
        self.alock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self.ilock:
            self.identity.executescript(IDENTITY_SCHEMA)
        with self.alock:
            self.analysis.executescript(ANALYSIS_SCHEMA)
            existing = self.analysis.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
            if existing == 0:
                self.analysis.executemany(
                    "INSERT INTO regions(region_code, seq) VALUES (?, ?)",
                    [(code, i) for i, code in enumerate(_region_codes(), start=1)],
                )

    def close(self):
        with self.ilock:
            self.identity.close()
        with self.alock:
            self.analysis.close()
