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
  result TEXT, cycle_id TEXT, account_type TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

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

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'DATA RETENTION POLICY: % on % is forbidden (append-only)', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['market_data','agent_analysis','ceo_decision','trade_history','learning_results',
                           'research_projects','agent_score','memory_records','improvement_history','version_control']
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I_append_only ON %I', t, t);
    EXECUTE format('CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON %I '
                   'FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()', t, t);
  END LOOP;
END $$;
