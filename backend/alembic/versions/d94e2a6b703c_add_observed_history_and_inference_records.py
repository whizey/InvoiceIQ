"""Observed financial history and durable inference telemetry; never invent past state."""
from alembic import op
revision = "d94e2a6b703c"
down_revision = "c83f1d5a602b"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE invoices, recovery_cases IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""CREATE TABLE analytics_history_state (
	name VARCHAR(40) NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (name)
)""")
    op.execute("""CREATE TABLE invoice_state_snapshots (
	id BIGSERIAL NOT NULL,
	invoice_id UUID NOT NULL,
	company_id UUID NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	source VARCHAR(30) NOT NULL,
	amount_total NUMERIC(14, 2) NOT NULL,
	amount_paid NUMERIC(14, 2) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	posting_date DATE NOT NULL,
	due_date DATE NOT NULL,
	status VARCHAR(30) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE CASCADE
)""")
    op.execute("""CREATE INDEX ix_invoice_snapshot_asof ON invoice_state_snapshots (company_id, observed_at, invoice_id)""")
    op.execute("""CREATE TABLE case_state_snapshots (
	id BIGSERIAL NOT NULL,
	case_id UUID NOT NULL,
	invoice_id UUID NOT NULL,
	company_id UUID NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(30) NOT NULL,
	source VARCHAR(30) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(case_id) REFERENCES recovery_cases (id) ON DELETE CASCADE
)""")
    op.execute("""CREATE INDEX ix_case_snapshot_asof ON case_state_snapshots (company_id, observed_at, case_id)""")
    op.execute("""CREATE TABLE inference_records (
	invoice_id UUID,
	company_id UUID,
	workspace_id VARCHAR(100),
	currency VARCHAR(3),
	model_kind VARCHAR(20) NOT NULL,
	model_name VARCHAR(100) NOT NULL,
	model_version VARCHAR(200),
	source VARCHAR(30) NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	latency_ms FLOAT,
	status VARCHAR(20) NOT NULL,
	error_code VARCHAR(40),
	probability FLOAT,
	metadata JSONB NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id)
)""")
    op.execute("""CREATE INDEX ix_inference_scope_time ON inference_records (company_id, started_at)""")
    op.execute("""CREATE INDEX ix_inference_workspace_time ON inference_records (workspace_id, started_at)""")
    # Baselines are observed NOW, never backdated to issue/creation dates.
    op.execute("""INSERT INTO invoice_state_snapshots
        (invoice_id,company_id,observed_at,source,amount_total,amount_paid,currency,posting_date,due_date,status)
        SELECT id,company_id,clock_timestamp(),'migration_baseline',amount_total,amount_paid,currency,
               COALESCE(posting_date,issue_date),due_date,status::text FROM invoices""")
    op.execute("""INSERT INTO case_state_snapshots (case_id,invoice_id,company_id,observed_at,opened_at,status,source)
        SELECT id,invoice_id,company_id,clock_timestamp(),opened_at,status::text,'migration_baseline' FROM recovery_cases""")
    # Existing known predictions retain their actual timestamps, without invented request latency.
    op.execute("""INSERT INTO inference_records
        (id,invoice_id,company_id,currency,model_kind,model_name,model_version,source,started_at,completed_at,status,probability,metadata)
        SELECT gen_random_uuid(),id,company_id,currency,'b2b',COALESCE(payment_risk_model,'unknown'),payment_risk_model_version,
        'legacy_snapshot',payment_risk_scored_at,payment_risk_scored_at,'SUCCEEDED',late_payment_probability,
        '{"provenance":"existing stored invoice score; request telemetry unavailable"}'::jsonb FROM invoices
        WHERE payment_risk_scored_at IS NOT NULL AND payment_risk_model_version IS NOT NULL
        AND late_payment_probability BETWEEN 0 AND 1""")
    op.execute("""CREATE FUNCTION invoiceiq_capture_invoice_state() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' OR ROW(NEW.company_id,NEW.amount_total,NEW.amount_paid,NEW.currency,NEW.posting_date,NEW.issue_date,NEW.due_date,NEW.status)
        IS DISTINCT FROM ROW(OLD.company_id,OLD.amount_total,OLD.amount_paid,OLD.currency,OLD.posting_date,OLD.issue_date,OLD.due_date,OLD.status) THEN
        INSERT INTO invoice_state_snapshots
          (invoice_id,company_id,observed_at,source,amount_total,amount_paid,currency,posting_date,due_date,status)
          VALUES (NEW.id,NEW.company_id,clock_timestamp(),'recorded_change',NEW.amount_total,NEW.amount_paid,
                  NEW.currency,COALESCE(NEW.posting_date,NEW.issue_date),NEW.due_date,NEW.status::text);
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER invoiceiq_invoice_history AFTER INSERT OR UPDATE ON invoices FOR EACH ROW EXECUTE FUNCTION invoiceiq_capture_invoice_state()")
    op.execute("""CREATE FUNCTION invoiceiq_capture_case_state() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' OR ROW(NEW.status,NEW.company_id,NEW.invoice_id,NEW.opened_at)
        IS DISTINCT FROM ROW(OLD.status,OLD.company_id,OLD.invoice_id,OLD.opened_at) THEN
        INSERT INTO case_state_snapshots (case_id,invoice_id,company_id,observed_at,opened_at,status,source)
          VALUES (NEW.id,NEW.invoice_id,NEW.company_id,clock_timestamp(),NEW.opened_at,NEW.status::text,'recorded_change');
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER invoiceiq_case_history AFTER INSERT OR UPDATE ON recovery_cases FOR EACH ROW EXECUTE FUNCTION invoiceiq_capture_case_state()")
    # Coverage begins only once every baseline and trigger has been installed.
    op.execute("INSERT INTO analytics_history_state VALUES ('financial_snapshots', clock_timestamp()), ('inference_records', clock_timestamp())")


def downgrade():
    op.execute("DROP TRIGGER invoiceiq_case_history ON recovery_cases")
    op.execute("DROP TRIGGER invoiceiq_invoice_history ON invoices")
    op.execute("DROP FUNCTION invoiceiq_capture_case_state()")
    op.execute("DROP FUNCTION invoiceiq_capture_invoice_state()")
    for table in ('inference_records','case_state_snapshots','invoice_state_snapshots','analytics_history_state'):
        op.drop_table(table)
