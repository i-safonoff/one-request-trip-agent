"""one order per cart

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28 20:10:00.000000
"""
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch:  # batch mode: SQLite cannot ADD CONSTRAINT
        batch.create_unique_constraint('uq_orders_cart_id', ['cart_id'])


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.drop_constraint('uq_orders_cart_id', type_='unique')
