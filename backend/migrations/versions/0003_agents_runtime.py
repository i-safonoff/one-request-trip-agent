"""agents runtime

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28 15:52:20.988251
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('carts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('chat_id', sa.Uuid(), nullable=False),
    sa.Column('plan_revision', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('items', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('total', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('price_changes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_carts_chat_id'), 'carts', ['chat_id'], unique=False)
    op.create_table('offers',
    sa.Column('chat_id', sa.Uuid(), nullable=False),
    sa.Column('offer_id', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=24), nullable=False),
    sa.Column('data_mode', sa.String(length=8), nullable=False),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ),
    sa.PrimaryKeyConstraint('chat_id', 'offer_id')
    )
    op.create_table('orders',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.String(length=16), nullable=False),
    sa.Column('cart_id', sa.Uuid(), nullable=False),
    sa.Column('chat_id', sa.Uuid(), nullable=False),
    sa.Column('guest', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['cart_id'], ['carts.id'], ),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('number')
    )
    op.create_index(op.f('ix_orders_chat_id'), 'orders', ['chat_id'], unique=False)
    op.create_table('plan_operations',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('chat_id', sa.Uuid(), nullable=False),
    sa.Column('plan_id', sa.Uuid(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('actor', sa.String(length=24), nullable=False),
    sa.Column('op', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], ),
    sa.ForeignKeyConstraint(['plan_id'], ['trip_plans.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_plan_operations_chat_id'), 'plan_operations', ['chat_id'], unique=False)
    op.create_index(op.f('ix_plan_operations_plan_id'), 'plan_operations', ['plan_id'], unique=False)
    op.add_column('agent_runs', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('agent_runs', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_runs', sa.Column('checkpoint', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))
    op.create_index('agent_runs_one_running', 'agent_runs', ['chat_id'], unique=True, postgresql_where=sa.text("status = 'running'"), sqlite_where=sa.text("status = 'running'"))
    op.add_column('chat_messages', sa.Column('turn', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))
    op.add_column('trip_plan_versions', sa.Column('label', sa.String(length=120), nullable=True))
    op.add_column('trip_plan_versions', sa.Column('run_id', sa.Uuid(), nullable=True))
    op.add_column('users', sa.Column('is_guest', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('users') as batch:  # batch mode: SQLite cannot ALTER COLUMN
        batch.alter_column('telegram_id', existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.alter_column('telegram_id', existing_type=sa.BigInteger(), nullable=False)
    op.drop_column('users', 'is_guest')
    op.drop_column('trip_plan_versions', 'run_id')
    op.drop_column('trip_plan_versions', 'label')
    op.drop_column('chat_messages', 'turn')
    op.drop_index('agent_runs_one_running', table_name='agent_runs', postgresql_where=sa.text("status = 'running'"), sqlite_where=sa.text("status = 'running'"))
    op.drop_column('agent_runs', 'checkpoint')
    op.drop_column('agent_runs', 'heartbeat_at')
    op.drop_column('agent_runs', 'attempts')
    op.drop_index(op.f('ix_plan_operations_plan_id'), table_name='plan_operations')
    op.drop_index(op.f('ix_plan_operations_chat_id'), table_name='plan_operations')
    op.drop_table('plan_operations')
    op.drop_index(op.f('ix_orders_chat_id'), table_name='orders')
    op.drop_table('orders')
    op.drop_table('offers')
    op.drop_index(op.f('ix_carts_chat_id'), table_name='carts')
    op.drop_table('carts')
