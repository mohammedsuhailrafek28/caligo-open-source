"""R3 payment observations and merchant ledger imports

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


# Both tables are append-only (SQLite triggers; the ORM guard in app.models.make_append_only covers every backend).
APPEND_ONLY_TABLES = ("merchant_ledger_imports", "payment_observations")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.create_table('merchant_ledger_imports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('file_sha256', sa.String(length=64), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('recorded_count', sa.Integer(), nullable=False),
    sa.Column('duplicate_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('row_count >= 1 AND recorded_count >= 0 AND duplicate_count >= 0 AND recorded_count + duplicate_count = row_count', name='ck_merchant_ledger_imports_counts'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'file_sha256', name='uq_merchant_ledger_imports_project_file')
    )
    with op.batch_alter_table('merchant_ledger_imports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_merchant_ledger_imports_project_id'), ['project_id'], unique=False)

    op.create_table('payment_observations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('payment_id', sa.String(length=36), nullable=False),
    sa.Column('import_id', sa.String(length=36), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('confidence', sa.String(length=24), nullable=False),
    sa.Column('actor', sa.String(length=16), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('source_event_id', sa.String(length=100), nullable=True),
    sa.Column('source_reference', sa.String(length=100), nullable=True),
    sa.Column('provider_state', sa.String(length=24), nullable=True),
    sa.Column('instrument', sa.String(length=16), nullable=True),
    sa.Column('asserted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.Column('provenance', sa.Text(), nullable=False),
    sa.Column('note', sa.String(length=200), nullable=True),
    sa.CheckConstraint("(kind = 'INVOICE_STATE') = (provider_state IS NOT NULL)", name='ck_payment_observations_state_kind'),
    sa.CheckConstraint("(source = 'MERCHANT_IMPORT') = (import_id IS NOT NULL)", name='ck_payment_observations_import_link'),
    sa.CheckConstraint("(source = 'PROVIDER_API') = (confidence = 'PROVIDER_EVIDENCE')", name='ck_payment_observations_provider_confidence'),
    sa.CheckConstraint("(source = 'WEBHOOK') = (confidence = 'UNCONFIRMED_WEBHOOK')", name='ck_payment_observations_webhook_confidence'),
    sa.CheckConstraint("(source IN ('MERCHANT_IMPORT', 'SELLER_ENTRY')) = (actor = 'SELLER')", name='ck_payment_observations_actor_matches_source'),
    sa.CheckConstraint("(source IN ('MERCHANT_IMPORT', 'SELLER_ENTRY')) = (confidence = 'UNVERIFIED_CLAIM')", name='ck_payment_observations_claim_confidence'),
    sa.CheckConstraint("actor IN ('SELLER', 'SYSTEM')", name='ck_payment_observations_actor'),
    sa.CheckConstraint("confidence IN ('PROVIDER_EVIDENCE', 'UNCONFIRMED_WEBHOOK', 'UNVERIFIED_CLAIM')", name='ck_payment_observations_confidence'),
    sa.CheckConstraint("kind != 'INVOICE_STATE' OR source = 'PROVIDER_API'", name='ck_payment_observations_state_from_provider'),
    sa.CheckConstraint("kind IN ('PAYMENT', 'REFUND', 'INVOICE_STATE')", name='ck_payment_observations_kind'),
    sa.CheckConstraint("source IN ('PROVIDER_API', 'WEBHOOK', 'MERCHANT_IMPORT', 'SELLER_ENTRY')", name='ck_payment_observations_source'),
    sa.CheckConstraint('amount_minor >= 0', name='ck_payment_observations_amount_nonneg'),
    sa.CheckConstraint('length(currency) = 3 AND currency = upper(currency)', name='ck_payment_observations_currency'),
    sa.ForeignKeyConstraint(['import_id'], ['merchant_ledger_imports.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['payment_records.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'dedupe_key', name='uq_payment_observations_project_dedupe')
    )
    with op.batch_alter_table('payment_observations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payment_observations_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_observations_payment_id'), ['payment_id'], unique=False)
        batch_op.create_index('ix_payment_observations_source_event', ['project_id', 'source_event_id'], unique=False)

    if _is_sqlite():
        for table in APPEND_ONLY_TABLES:
            for verb in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                           f"BEGIN SELECT RAISE(ABORT, '{table} are append-only'); END;")


def _refuse_if_evidence_exists(*tables: str) -> None:
    """Downgrading drops these append-only evidence tables: refuse while they hold rows (export them first)."""
    bind = op.get_bind()
    for table in tables:
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError(f"refusing to downgrade: {table} holds evidence records; export them and remove them deliberately first")


def downgrade() -> None:
    _refuse_if_evidence_exists(*reversed(APPEND_ONLY_TABLES))
    if _is_sqlite():
        for table in APPEND_ONLY_TABLES:
            for verb in ("update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{verb}")
    op.drop_table('payment_observations')
    op.drop_table('merchant_ledger_imports')
