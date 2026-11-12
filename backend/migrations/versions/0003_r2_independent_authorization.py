"""R2 independent authorization of payment-instruction changes

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


# Append-only (SQLite triggers; the ORM guard in app.models.make_append_only covers every backend).
# instruction_verification_challenges is NOT append-only: only failed_attempts / consumed_at change, via the verification service.
APPEND_ONLY_TABLES = ("buyer_authenticators", "authenticator_revocations", "totp_uses", "instruction_change_verifications")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.create_table('buyer_authenticators',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('buyer_email', sa.String(length=254), nullable=False),
    sa.Column('secret_ciphertext', sa.Text(), nullable=False),
    sa.Column('key_id', sa.String(length=32), nullable=False),
    sa.Column('trust_basis', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("trust_basis IN ('OPERATOR_ESTABLISHED')", name='ck_buyer_authenticators_trust_basis'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('buyer_authenticators', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_buyer_authenticators_project_id'), ['project_id'], unique=False)

    op.create_table('authenticator_revocations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('authenticator_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['authenticator_id'], ['buyer_authenticators.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('authenticator_id')
    )
    with op.batch_alter_table('authenticator_revocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_authenticator_revocations_project_id'), ['project_id'], unique=False)

    op.create_table('instruction_verification_challenges',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('change_id', sa.String(length=36), nullable=False),
    sa.Column('authenticator_id', sa.String(length=36), nullable=False),
    sa.Column('approver_email', sa.String(length=254), nullable=False),
    sa.Column('from_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('proposed_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('failed_attempts', sa.Integer(), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['authenticator_id'], ['buyer_authenticators.id'], ),
    sa.ForeignKeyConstraint(['change_id'], ['instruction_change_requests.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('instruction_verification_challenges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_instruction_verification_challenges_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_instruction_verification_challenges_change_id'), ['change_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_instruction_verification_challenges_authenticator_id'), ['authenticator_id'], unique=False)

    op.create_table('totp_uses',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('authenticator_id', sa.String(length=36), nullable=False),
    sa.Column('step', sa.Integer(), nullable=False),
    sa.Column('challenge_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['authenticator_id'], ['buyer_authenticators.id'], ),
    sa.ForeignKeyConstraint(['challenge_id'], ['instruction_verification_challenges.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('authenticator_id', 'step', name='uq_totp_uses_authenticator_step')
    )

    op.create_table('instruction_change_verifications',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('change_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('authenticator_id', sa.String(length=36), nullable=False),
    sa.Column('challenge_id', sa.String(length=36), nullable=False),
    sa.Column('approver_email', sa.String(length=254), nullable=False),
    sa.Column('from_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('proposed_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('trust_basis', sa.String(length=32), nullable=False),
    sa.Column('applied_instruction_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(outcome = 'APPLIED') = (applied_instruction_id IS NOT NULL)", name='ck_instruction_verifications_applied'),
    sa.CheckConstraint("outcome IN ('APPLIED', 'REJECTED')", name='ck_instruction_verifications_outcome'),
    sa.CheckConstraint("trust_basis IN ('OPERATOR_ESTABLISHED')", name='ck_instruction_verifications_trust_basis'),
    sa.ForeignKeyConstraint(['applied_instruction_id'], ['payment_instructions.id'], ),
    sa.ForeignKeyConstraint(['authenticator_id'], ['buyer_authenticators.id'], ),
    sa.ForeignKeyConstraint(['challenge_id'], ['instruction_verification_challenges.id'], ),
    sa.ForeignKeyConstraint(['change_id'], ['instruction_change_requests.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('change_id'),
    sa.UniqueConstraint('challenge_id')
    )
    with op.batch_alter_table('instruction_change_verifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_instruction_change_verifications_project_id'), ['project_id'], unique=False)

    if _is_sqlite():
        for table in APPEND_ONLY_TABLES:
            for verb in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                           f"BEGIN SELECT RAISE(ABORT, '{table} are append-only'); END;")


def downgrade() -> None:
    if _is_sqlite():
        for table in APPEND_ONLY_TABLES:
            for verb in ("update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{verb}")
    op.drop_table('instruction_change_verifications')
    op.drop_table('totp_uses')
    op.drop_table('instruction_verification_challenges')
    op.drop_table('authenticator_revocations')
    op.drop_table('buyer_authenticators')
