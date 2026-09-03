"""user_secrets (optional third-party keys) and users.awaiting_secret

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03 21:10:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_secrets',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('service', sa.String(length=32), nullable=False),
        sa.Column('key_enc', sa.Text(), nullable=False),
        sa.Column('last4', sa.String(length=4), nullable=False),
        sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'service', name='uq_user_secrets_user_service'),
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('awaiting_secret', sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('awaiting_secret')
    op.drop_table('user_secrets')
