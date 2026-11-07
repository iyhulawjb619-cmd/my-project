-- ISHIBASHI AI HEDGE FUND OS v1.0 — PostgreSQL schema
-- createdb ai_hedge_fund && psql ai_hedge_fund -f db/schema.sql
-- DATA RETENTION POLICY: 削除禁止・上書き禁止 → 全テーブルで UPDATE / DELETE をトリガーで拒否する。

CREATE TABLE IF NOT EXISTS market_data (
  id BIGSERIAL PRIMARY KEY, timestamp TEXT, symbol TEXT, timeframe TEXT,
  open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION, close DOUBLE PRECISION,
  volume DOUBLE PRECISION, spread DOUBLE PRECISION, atr DOUBLE PRECISION, adx DOUBLE PRECISION,
  rsi DOUBLE PRECISION, macd DOUBLE PRECISION, ema DOUBLE PRECISION, sma DOUBLE PRECISION,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_analysis (
  id BIGSERIAL PRIMARY KEY, cycle_id TEXT, timestamp TEXT, agent_name TEXT, symbol TEXT,
  decision TEXT CHECK (decision IN ('BUY','SELL','WAIT')),
  confidence INTEGER CHECK (confidence BETWEEN 0 AND 100),
  risk_score INTEGER CHECK (risk_score BETWEEN 0 AND 100),
  reasoning TEXT, main_scenario TEXT, alternative_scenario TEXT, worst_scenario TEXT,
  report_json TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_agent_analysis_cycle ON agent_analysis (cycle_id);

CREATE TABLE IF NOT EXISTS ceo_decision (
  id BIGSERIAL PRIMARY KEY, cycle_id TEXT, timestamp TEXT, symbol TEXT,
  decision TEXT CHECK (decision IN ('BUY','SELL','WAIT')),
  confidence INTEGER CHECK (confidence BETWEEN 0 AND 100),
  reason TEXT, expected_return TEXT, expected_risk TEXT, mode TEXT, report_json TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ceo_decision_cycle ON ceo_decision (cycle_id);

CREATE TABLE IF NOT EXISTS trade_history (
  id BIGSERIAL PRIMARY KEY, timestamp TEXT, symbol TEXT, direction TEXT,
  entry_price DOUBLE PRECISION, exit_price DOUBLE PRECISION, stop_loss DOUBLE PRECISION,
  take_profit DOUBLE PRECISION, lot DOUBLE PRECISION, profit_loss DOUBLE PRECISION, rr DOUBLE PRECISION,
  result TEXT, cycle_id TEXT, account_type TEXT, strategy TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

-- v1.0 初期版からの移行
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS strategy TEXT;

CREATE TABLE IF NOT EXISTS learning_results (
  id BIGSERIAL PRIMARY KEY, timestamp TEXT, finding TEXT, improvement TEXT, estimated_effect TEXT,
  priority TEXT, status TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research_projects (
  id BIGSERIAL PRIMARY KEY, title TEXT, hypothesis TEXT, methodology TEXT, result TEXT,
  recommendation TEXT, status TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_score (
  id BIGSERIAL PRIMARY KEY, agent_name TEXT, accuracy DOUBLE PRECISION,
  profit_contribution DOUBLE PRECISION, risk_contribution DOUBLE PRECISION, evaluation_date TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_records (
  id BIGSERIAL PRIMARY KEY, trade_id TEXT, timestamp TEXT, market_state TEXT, agent_opinions TEXT,
  ceo_decision TEXT, risk_decision TEXT, entry DOUBLE PRECISION, exit DOUBLE PRECISION, pl DOUBLE PRECISION,
  lessons_learned TEXT, improvement_suggestions TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS improvement_history (
  id BIGSERIAL PRIMARY KEY, proposal_id TEXT, stage TEXT, agent TEXT, status TEXT, payload_json TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_improvement_proposal ON improvement_history (proposal_id);

CREATE TABLE IF NOT EXISTS version_control (
  id BIGSERIAL PRIMARY KEY, version TEXT, author TEXT, date TEXT, reason TEXT, changes TEXT,
  expected_impact TEXT, actual_impact TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reports (
  id BIGSERIAL PRIMARY KEY, kind TEXT, period TEXT, body TEXT, payload_json TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_reports_kind ON reports (kind);

-- v1.1 拡張（統合）: すべて追記専用。状態の変化は行の追加で表す。
-- trade_journal の account_type='REAL_IMPORT'（11 文字）は TEXT 列なのでそのまま入る。

CREATE TABLE IF NOT EXISTS user_positions (
  id BIGSERIAL PRIMARY KEY, event TEXT CHECK (event IN ('register','close')), position_id TEXT, symbol TEXT,
  direction TEXT, units DOUBLE PRECISION, entry DOUBLE PRECISION, sl DOUBLE PRECISION, tp DOUBLE PRECISION,
  opened_at TEXT, closed_at TEXT, exit_price DOUBLE PRECISION, note TEXT, payload_json TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_user_positions_pid ON user_positions (position_id);

CREATE TABLE IF NOT EXISTS notifications (
  id BIGSERIAL PRIMARY KEY, key TEXT, code TEXT, text TEXT, severity TEXT, at TEXT, position_id TEXT,
  suppressed INTEGER, suppressed_reason TEXT, payload_json TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_notifications_key ON notifications (key);

CREATE TABLE IF NOT EXISTS level_watch (
  id BIGSERIAL PRIMARY KEY, version_id TEXT, symbol TEXT, report_kind TEXT, watch_created_at TEXT,
  current DOUBLE PRECISION, atr DOUBLE PRECISION, range_low DOUBLE PRECISION, range_high DOUBLE PRECISION,
  payload_json TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_level_watch_version ON level_watch (version_id);

-- level_scoring.LevelWatchRepository が使う検証・予測結果の表
CREATE TABLE IF NOT EXISTS level_watch_result (
  id BIGSERIAL PRIMARY KEY, version_id TEXT, validated_at TEXT, payload_json TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prediction (
  id BIGSERIAL PRIMARY KEY, prediction_id TEXT, symbol TEXT, made_at TEXT, deadline TEXT, payload_json TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prediction_result (
  id BIGSERIAL PRIMARY KEY, prediction_id TEXT, verdict TEXT, reason TEXT, judged_at TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calendar_events (
  id BIGSERIAL PRIMARY KEY, batch_id TEXT, fetched_at TEXT, event_id TEXT, date_jst TEXT, date_utc TEXT,
  country TEXT, title TEXT, title_ja TEXT, importance INTEGER, forecast DOUBLE PRECISION,
  previous DOUBLE PRECISION, actual DOUBLE PRECISION, unit TEXT, rank TEXT, source TEXT, payload_json TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_calendar_events_batch ON calendar_events (batch_id);

CREATE TABLE IF NOT EXISTS news_items (
  id BIGSERIAL PRIMARY KEY, batch_id TEXT, fetched_at TEXT, item_id TEXT, title TEXT, provider TEXT,
  published_jst TEXT, urgency INTEGER, link TEXT, tags TEXT, payload_json TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_news_items_batch ON news_items (batch_id);

CREATE TABLE IF NOT EXISTS macro_series (
  id BIGSERIAL PRIMARY KEY, symbol TEXT, timestamp TEXT, value DOUBLE PRECISION, close DOUBLE PRECISION,
  open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION, source TEXT, unit TEXT, payload_json TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_macro_series_symbol ON macro_series (symbol);

CREATE TABLE IF NOT EXISTS predictions (
  id BIGSERIAL PRIMARY KEY, prediction_id TEXT, symbol TEXT, kind TEXT, made_at TEXT, deadline TEXT, text TEXT,
  verdict TEXT, payload_json TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_predictions_pid ON predictions (prediction_id);

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'DATA RETENTION POLICY: % on % is forbidden (append-only)', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['market_data','agent_analysis','ceo_decision','trade_history','learning_results',
                           'research_projects','agent_score','memory_records','improvement_history','version_control','reports',
                           'user_positions','notifications','level_watch','level_watch_result','prediction','prediction_result',
                           'calendar_events','news_items','macro_series','predictions']
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I_append_only ON %I', t, t);
    EXECUTE format('CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON %I '
                   'FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()', t, t);
  END LOOP;
END $$;
