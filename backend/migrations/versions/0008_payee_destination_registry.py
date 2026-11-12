"""Payee destination registry: append-only, seller-asserted destination versions per project

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

TABLE = "payee_destination_versions"


def upgrade() -> None:
    op.create_table(TABLE,
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('payee_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=120), nullable=False),
    sa.Column('destination', sa.String(length=254), nullable=False),
    sa.Column('destination_sha256', sa.String(length=64), nullable=False),
    sa.Column('previous_version_id', sa.String(length=36), nullable=True),
    sa.Column('chain_sha256', sa.String(length=64), nullable=False),
    sa.Column('provenance', sa.String(length=16), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('created_by_seller_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(version = 1) = (previous_version_id IS NULL)", name='ck_payee_versions_chain'),
    sa.CheckConstraint("provenance = 'SELLER_ASSERTED'", name='ck_payee_versions_provenance'),
    sa.CheckConstraint('version >= 1', name='ck_payee_versions_version'),
    sa.ForeignKeyConstraint(['created_by_seller_id'], ['sellers.id'], ),
    sa.ForeignKeyConstraint(['previous_version_id'], [f'{TABLE}.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'payee_id', 'version', name='uq_payee_versions_project_payee_version')
    )
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payee_destination_versions_payee_id'), ['payee_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payee_destination_versions_project_id'), ['project_id'], unique=False)

    if op.get_bind().dialect.name == "sqlite":
        for verb in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER {TABLE}_no_{verb.lower()} BEFORE {verb} ON {TABLE} "
                       f"BEGIN SELECT RAISE(ABORT, '{TABLE} are append-only'); END;")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar():
        raise RuntimeError(f"refusing to downgrade: {TABLE} holds registry history; export it and remove it deliberately first")
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payee_destination_versions_project_id'))
        batch_op.drop_index(batch_op.f('ix_payee_destination_versions_payee_id'))
    op.drop_table(TABLE)
