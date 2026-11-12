"""S1: simulated merchant state (orders, inventory, fulfillment, incoming events, audit) with database-level integrity triggers

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

TABLES = ("sim_inventory_items", "sim_incoming_events", "sim_orders", "sim_fulfillments", "sim_inventory_movements", "sim_audit_records")
APPEND_ONLY = ('sim_inventory_items', 'sim_incoming_events', 'sim_inventory_movements', 'sim_audit_records')
# Frozen copies of the trigger SQL as of this revision (do not import app code: a later change needs its own migration).
TRIGGERS = [
    "CREATE TRIGGER sim_orders_payment_id_same_project BEFORE INSERT ON sim_orders WHEN NEW.payment_id IS NOT NULL AND (SELECT project_id FROM payment_records WHERE id = NEW.payment_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_orders.payment_id must reference the same project'); END;",
    "CREATE TRIGGER sim_orders_item_id_same_project BEFORE INSERT ON sim_orders WHEN NEW.item_id IS NOT NULL AND (SELECT project_id FROM sim_inventory_items WHERE id = NEW.item_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_orders.item_id must reference the same project'); END;",
    "CREATE TRIGGER sim_orders_identity_immutable BEFORE UPDATE ON sim_orders WHEN NEW.project_id IS NOT OLD.project_id OR NEW.payment_id IS NOT OLD.payment_id OR NEW.business_key IS NOT OLD.business_key OR NEW.item_id IS NOT OLD.item_id OR NEW.quantity IS NOT OLD.quantity OR NEW.amount_minor IS NOT OLD.amount_minor OR NEW.currency IS NOT OLD.currency BEGIN SELECT RAISE(ABORT, 'sim_orders identity is immutable'); END;",
    "CREATE TRIGGER sim_fulfillments_order_id_same_project BEFORE INSERT ON sim_fulfillments WHEN NEW.order_id IS NOT NULL AND (SELECT project_id FROM sim_orders WHERE id = NEW.order_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_fulfillments.order_id must reference the same project'); END;",
    "CREATE TRIGGER sim_fulfillments_identity_immutable BEFORE UPDATE ON sim_fulfillments WHEN NEW.project_id IS NOT OLD.project_id OR NEW.order_id IS NOT OLD.order_id OR NEW.operation_key IS NOT OLD.operation_key BEGIN SELECT RAISE(ABORT, 'sim_fulfillments identity is immutable'); END;",
    "CREATE TRIGGER sim_fulfillments_dispatch_is_final BEFORE UPDATE ON sim_fulfillments WHEN OLD.state = 'DISPATCHED' AND (NEW.state IS NOT 'DISPATCHED' OR NEW.dispatched_at IS NOT OLD.dispatched_at) BEGIN SELECT RAISE(ABORT, 'a dispatched fulfillment cannot be changed'); END;",
    "CREATE TRIGGER sim_incoming_events_payment_id_same_project BEFORE INSERT ON sim_incoming_events WHEN NEW.payment_id IS NOT NULL AND (SELECT project_id FROM payment_records WHERE id = NEW.payment_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_incoming_events.payment_id must reference the same project'); END;",
    "CREATE TRIGGER sim_incoming_events_replay_of_id_same_project BEFORE INSERT ON sim_incoming_events WHEN NEW.replay_of_id IS NOT NULL AND (SELECT project_id FROM sim_incoming_events WHERE id = NEW.replay_of_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_incoming_events.replay_of_id must reference the same project'); END;",
    "CREATE TRIGGER sim_inventory_movements_item_id_same_project BEFORE INSERT ON sim_inventory_movements WHEN NEW.item_id IS NOT NULL AND (SELECT project_id FROM sim_inventory_items WHERE id = NEW.item_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_inventory_movements.item_id must reference the same project'); END;",
    "CREATE TRIGGER sim_inventory_movements_order_id_same_project BEFORE INSERT ON sim_inventory_movements WHEN NEW.order_id IS NOT NULL AND (SELECT project_id FROM sim_orders WHERE id = NEW.order_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_inventory_movements.order_id must reference the same project'); END;",
    "CREATE TRIGGER sim_inventory_movements_source_event_id_same_project BEFORE INSERT ON sim_inventory_movements WHEN NEW.source_event_id IS NOT NULL AND (SELECT project_id FROM sim_incoming_events WHERE id = NEW.source_event_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_inventory_movements.source_event_id must reference the same project'); END;",
    "CREATE TRIGGER sim_audit_records_event_id_same_project BEFORE INSERT ON sim_audit_records WHEN NEW.event_id IS NOT NULL AND (SELECT project_id FROM sim_incoming_events WHERE id = NEW.event_id) IS NOT NEW.project_id BEGIN SELECT RAISE(ABORT, 'sim_audit_records.event_id must reference the same project'); END;",
]



def _refuse_if_evidence_exists(*tables: str) -> None:
    """Downgrading drops these simulated evidence tables: refuse while they hold rows (export them first)."""
    bind = op.get_bind()
    for table in tables:
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError(f"refusing to downgrade: {table} holds simulated merchant records; export them and remove them deliberately first")


def upgrade() -> None:
    op.create_table('sim_inventory_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('initial_stock', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('initial_stock >= 0', name='ck_sim_inventory_items_stock'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'sku', name='uq_sim_inventory_items_project_sku')
    )
    with op.batch_alter_table('sim_inventory_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sim_inventory_items_project_id'), ['project_id'], unique=False)

    op.create_table('sim_incoming_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('payment_id', sa.String(length=36), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('event_key', sa.String(length=100), nullable=False),
    sa.Column('provider_txn_ref', sa.String(length=100), nullable=True),
    sa.Column('event_type', sa.String(length=24), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('asserted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('replay_status', sa.String(length=28), nullable=False),
    sa.Column('replay_of_id', sa.String(length=36), nullable=True),
    sa.CheckConstraint("(replay_status = 'FIRST') = (replay_of_id IS NULL)", name='ck_sim_incoming_events_replay_link'),
    sa.CheckConstraint("event_type IN ('PAYMENT_COMPLETED', 'PAYMENT_REFUNDED', 'PAYMENT_FAILED')", name='ck_sim_incoming_events_type'),
    sa.CheckConstraint("replay_status IN ('FIRST', 'REPLAY_IDENTICAL', 'REPLAY_DIFFERENT_PAYLOAD')", name='ck_sim_incoming_events_replay'),
    sa.CheckConstraint("source IN ('SIMULATED_PROVIDER_WEBHOOK', 'SIMULATED_MERCHANT_JOB')", name='ck_sim_incoming_events_source'),
    sa.CheckConstraint("substr(source, 1, 10) = 'SIMULATED_'", name='ck_sim_incoming_events_simulated'),
    sa.CheckConstraint('amount_minor IS NULL OR amount_minor >= 0', name='ck_sim_incoming_events_amount'),
    sa.CheckConstraint('currency IS NULL OR (length(currency) = 3 AND currency = upper(currency))', name='ck_sim_incoming_events_currency'),
    sa.ForeignKeyConstraint(['payment_id'], ['payment_records.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['replay_of_id'], ['sim_incoming_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sim_incoming_events', schema=None) as batch_op:
        batch_op.create_index('ix_sim_incoming_events_key', ['project_id', 'source', 'event_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_incoming_events_payment_id'), ['payment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_incoming_events_project_id'), ['project_id'], unique=False)

    op.create_table('sim_orders',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('payment_id', sa.String(length=36), nullable=False),
    sa.Column('business_key', sa.String(length=64), nullable=False),
    sa.Column('item_id', sa.String(length=36), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("state IN ('PENDING', 'PAID', 'FAILED', 'FULFILLED', 'REFUNDED', 'CANCELLED')", name='ck_sim_orders_state'),
    sa.CheckConstraint('length(currency) = 3 AND currency = upper(currency)', name='ck_sim_orders_currency'),
    sa.CheckConstraint('quantity >= 1 AND amount_minor >= 1', name='ck_sim_orders_positive'),
    sa.ForeignKeyConstraint(['item_id'], ['sim_inventory_items.id'], ),
    sa.ForeignKeyConstraint(['payment_id'], ['payment_records.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'business_key', name='uq_sim_orders_project_business_key')
    )
    with op.batch_alter_table('sim_orders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sim_orders_payment_id'), ['payment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_orders_project_id'), ['project_id'], unique=False)

    op.create_table('sim_audit_records',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('event_id', sa.String(length=36), nullable=True),
    sa.Column('entity_type', sa.String(length=24), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=True),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('outcome', sa.String(length=40), nullable=True),
    sa.Column('processor', sa.String(length=16), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(action = 'EVENT_PROCESSED') = (outcome IS NOT NULL)", name='ck_sim_audit_records_outcome_link'),
    sa.CheckConstraint("action IN ('ORDER_CREATED', 'ORDER_STATE_CHANGED', 'INVENTORY_MOVEMENT', 'FULFILLMENT_CREATED', 'FULFILLMENT_DISPATCHED', 'FULFILLMENT_CANCELLED', 'DISPATCH_PROTECTED', 'EVENT_PROCESSED', 'OPERATION_DUPLICATE_SUPPRESSED')", name='ck_sim_audit_records_action'),
    sa.CheckConstraint("outcome IS NULL OR outcome IN ('APPLIED', 'DUPLICATE_NO_EFFECT', 'REJECTED_STALE', 'REJECTED_AMOUNT_MISMATCH', 'REJECTED_CURRENCY_MISMATCH', 'REJECTED_UNKNOWN_PAYMENT', 'REJECTED_INCOMPLETE_EVIDENCE', 'REJECTED_CONFLICTING_REPLAY', 'REJECTED_CONTRADICTED_BY_PROVIDER', 'REJECTED_NO_MERCHANT_ORDER', 'HELD_AWAITING_PROVIDER_EVIDENCE', 'FAULTY_APPLIED')", name='ck_sim_audit_records_outcome'),
    sa.CheckConstraint("processor IN ('SAFE', 'FAULTY_FIXTURE')", name='ck_sim_audit_records_processor'),
    sa.ForeignKeyConstraint(['event_id'], ['sim_incoming_events.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sim_audit_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sim_audit_records_event_id'), ['event_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_audit_records_project_id'), ['project_id'], unique=False)

    op.create_table('sim_fulfillments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('order_id', sa.String(length=36), nullable=False),
    sa.Column('operation_key', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(state = 'DISPATCHED') = (dispatched_at IS NOT NULL)", name='ck_sim_fulfillments_dispatch_evidence'),
    sa.CheckConstraint("state IN ('PENDING', 'DISPATCHED', 'CANCELLED')", name='ck_sim_fulfillments_state'),
    sa.ForeignKeyConstraint(['order_id'], ['sim_orders.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'operation_key', name='uq_sim_fulfillments_project_operation')
    )
    with op.batch_alter_table('sim_fulfillments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sim_fulfillments_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_fulfillments_project_id'), ['project_id'], unique=False)

    op.create_table('sim_inventory_movements',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('item_id', sa.String(length=36), nullable=False),
    sa.Column('order_id', sa.String(length=36), nullable=True),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('operation_key', sa.String(length=100), nullable=False),
    sa.Column('source_event_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('RESERVE', 'DEDUCT', 'RELEASE')", name='ck_sim_inventory_movements_kind'),
    sa.CheckConstraint('quantity >= 1', name='ck_sim_inventory_movements_quantity'),
    sa.ForeignKeyConstraint(['item_id'], ['sim_inventory_items.id'], ),
    sa.ForeignKeyConstraint(['order_id'], ['sim_orders.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['source_event_id'], ['sim_incoming_events.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'operation_key', name='uq_sim_inventory_movements_project_operation')
    )
    with op.batch_alter_table('sim_inventory_movements', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sim_inventory_movements_item_id'), ['item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_inventory_movements_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sim_inventory_movements_project_id'), ['project_id'], unique=False)

    if op.get_bind().dialect.name == "sqlite":
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                           f"BEGIN SELECT RAISE(ABORT, '{table} are append-only'); END;")
        for sql in TRIGGERS:
            op.execute(sql)


def downgrade() -> None:
    _refuse_if_evidence_exists(*TABLES)
    with op.batch_alter_table('sim_inventory_movements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sim_inventory_movements_project_id'))
        batch_op.drop_index(batch_op.f('ix_sim_inventory_movements_order_id'))
        batch_op.drop_index(batch_op.f('ix_sim_inventory_movements_item_id'))

    op.drop_table('sim_inventory_movements')
    with op.batch_alter_table('sim_fulfillments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sim_fulfillments_project_id'))
        batch_op.drop_index(batch_op.f('ix_sim_fulfillments_order_id'))

    op.drop_table('sim_fulfillments')
    with op.batch_alter_table('sim_audit_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sim_audit_records_project_id'))
        batch_op.drop_index(batch_op.f('ix_sim_audit_records_event_id'))

    op.drop_table('sim_audit_records')
    with op.batch_alter_table('sim_orders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sim_orders_project_id'))
        batch_op.drop_index(batch_op.f('ix_sim_orders_payment_id'))

    op.drop_table('sim_orders')
    with op.batch_alter_table('sim_incoming_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sim_incoming_events_project_id'))
        batch_op.drop_index(batch_op.f('ix_sim_incoming_events_payment_id'))
        batch_op.drop_index('ix_sim_incoming_events_key')

    op.drop_table('sim_incoming_events')
    with op.batch_alter_table('sim_inventory_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sim_inventory_items_project_id'))

    op.drop_table('sim_inventory_items')
