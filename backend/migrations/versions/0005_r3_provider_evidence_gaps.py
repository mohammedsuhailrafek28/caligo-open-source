"""R3 acceptance: provider evidence gaps (provider values that could not be stored as observations)

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

TABLE = "provider_evidence_gaps"


def upgrade() -> None:
    op.create_table(TABLE,
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('payment_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=32), nullable=False),
    sa.Column('raw_sha256', sa.String(length=64), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.CheckConstraint("kind IN ('PAYMENT', 'REFUND', 'INVOICE_STATE')", name='ck_provider_evidence_gaps_kind'),
    sa.CheckConstraint("reason IN ('OVERSIZED_AMOUNT', 'NEGATIVE_AMOUNT', 'INVALID_CURRENCY', 'OVERLONG_TRANSACTION_ID')", name='ck_provider_evidence_gaps_reason'),
    sa.ForeignKeyConstraint(['payment_id'], ['payment_records.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'dedupe_key', name='uq_provider_evidence_gaps_project_dedupe')
    )
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_provider_evidence_gaps_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_provider_evidence_gaps_payment_id'), ['payment_id'], unique=False)
    if op.get_bind().dialect.name == "sqlite":
        for verb in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER {TABLE}_no_{verb.lower()} BEFORE {verb} ON {TABLE} "
                       f"BEGIN SELECT RAISE(ABORT, '{TABLE} are append-only'); END;")


def _refuse_if_evidence_exists(*tables: str) -> None:
    """Downgrading drops these append-only evidence tables: refuse while they hold rows (export them first)."""
    bind = op.get_bind()
    for table in tables:
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError(f"refusing to downgrade: {table} holds evidence records; export them and remove them deliberately first")


def downgrade() -> None:
    _refuse_if_evidence_exists(TABLE)
    if op.get_bind().dialect.name == "sqlite":
        for verb in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_{verb}")
    op.drop_table(TABLE)
