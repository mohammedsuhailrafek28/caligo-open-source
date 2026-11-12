"""S2: append-only guarded recovery plans and their event history, with integrity triggers

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

TABLES = ("sim_recovery_plans", "sim_recovery_events")
# Frozen copies of the trigger SQL as of this revision (do not import app code: a later change needs its own migration).
TRIGGERS = [
    "CREATE TRIGGER sim_recovery_plans_payment_id_same_project BEFORE INSERT ON sim_recovery_plans WHEN NEW.payment_id IS NOT NULL AND (SELECT project_id FROM payment_records WHERE id = NEW.payment_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_recovery_plans.payment_id must reference the same project'); END;",
    "CREATE TRIGGER sim_recovery_events_plan_id_same_project BEFORE INSERT ON sim_recovery_events WHEN NEW.plan_id IS NOT NULL AND (SELECT project_id FROM sim_recovery_plans WHERE id = NEW.plan_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_recovery_events.plan_id must reference the same project'); END;",
]



def _refuse_if_evidence_exists(*tables: str) -> None:
    """Downgrading drops these append-only recovery records: refuse while they hold rows (export them first)."""
    bind = op.get_bind()
    for table in tables:
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError(f"refusing to downgrade: {table} holds recovery records; export them and remove them deliberately first")


def upgrade() -> None:
    op.create_table('sim_recovery_plans',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('plan_key', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('payment_id', sa.String(length=36), nullable=True),
    sa.Column('status', sa.String(length=14), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.Column('provider_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('evidence_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('created_by_seller_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(kind = 'FALSE_HEALTHY') = (payment_id IS NULL)", name='ck_sim_recovery_plans_payment_link'),
    sa.CheckConstraint("kind IN ('DUPLICATE_ORDERS', 'PAID_NOT_APPLIED', 'STALE_PAID_AFTER_REFUND', 'FALSE_HEALTHY')", name='ck_sim_recovery_plans_kind'),
    sa.CheckConstraint("status IN ('PROPOSED', 'NEEDS_REVIEW', 'BLOCKED', 'ADVISORY')", name='ck_sim_recovery_plans_status'),
    sa.CheckConstraint('version >= 1', name='ck_sim_recovery_plans_version'),
    sa.ForeignKeyConstraint(['created_by_seller_id'], ['sellers.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['payment_records.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'plan_key', 'version', name='uq_sim_recovery_plans_key_version')
    )
    with op.batch_alter_table('sim_recovery_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sim_recovery_plans_payment_id'), ['payment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_recovery_plans_project_id'), ['project_id'], unique=False)

    op.create_table('sim_recovery_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('plan_id', sa.String(length=36), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=20), nullable=False),
    sa.Column('actor_seller_id', sa.String(length=36), nullable=False),
    sa.Column('approved_version', sa.Integer(), nullable=True),
    sa.Column('approved_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("event_type != 'APPROVED' OR (approved_fingerprint IS NOT NULL AND approved_version IS NOT NULL)", name='ck_sim_recovery_events_approval_binding'),
    sa.CheckConstraint("event_type IN ('PLAN_CREATED', 'APPROVED', 'APPROVAL_REFUSED', 'STEP_APPLIED', 'STEP_ALREADY_DONE', 'EXECUTED', 'EXECUTION_REFUSED')", name='ck_sim_recovery_events_type'),
    sa.CheckConstraint('seq >= 1', name='ck_sim_recovery_events_seq'),
    sa.ForeignKeyConstraint(['actor_seller_id'], ['sellers.id'], ),
    sa.ForeignKeyConstraint(['plan_id'], ['sim_recovery_plans.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan_id', 'seq', name='uq_sim_recovery_events_plan_seq')
    )
    with op.batch_alter_table('sim_recovery_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sim_recovery_events_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_recovery_events_project_id'), ['project_id'], unique=False)
        batch_op.create_index('uq_sim_recovery_events_one_approval', ['plan_id'], unique=True, sqlite_where=sa.text("event_type = 'APPROVED'"))
        batch_op.create_index('uq_sim_recovery_events_one_execution', ['plan_id'], unique=True, sqlite_where=sa.text("event_type = 'EXECUTED'"))

    if op.get_bind().dialect.name == "sqlite":
        for table in TABLES:
            for verb in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                           f"BEGIN SELECT RAISE(ABORT, '{table} are append-only'); END;")
        for sql in TRIGGERS:
            op.execute(sql)


def downgrade() -> None:
    _refuse_if_evidence_exists(*TABLES)
    with op.batch_alter_table('sim_recovery_events', schema=None) as batch_op:
        batch_op.drop_index('uq_sim_recovery_events_one_execution', sqlite_where=sa.text("event_type = 'EXECUTED'"))
        batch_op.drop_index('uq_sim_recovery_events_one_approval', sqlite_where=sa.text("event_type = 'APPROVED'"))
        batch_op.drop_index(batch_op.f('ix_sim_recovery_events_project_id'))
        batch_op.drop_index(batch_op.f('ix_sim_recovery_events_plan_id'))

    op.drop_table('sim_recovery_events')
    with op.batch_alter_table('sim_recovery_plans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sim_recovery_plans_project_id'))
        batch_op.drop_index(batch_op.f('ix_sim_recovery_plans_payment_id'))

    op.drop_table('sim_recovery_plans')
