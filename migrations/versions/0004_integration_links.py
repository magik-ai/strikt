"""integration_links, integrations.notified_at and integrations.data

What Strikt created in a provider's account (MyFitnessPal foods and diary entries), when the
user was told a connection broke, and provider state that is not a token.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05 09:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROVIDER = sa.Enum(
    'whoop', 'withings', 'apple_health', 'myfitnesspal',
    name='provider', native_enum=False, length=32,
)
JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('integrations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('data', JSON, nullable=True))
    # connections that died before this release are old news: no wave of "disconnected" messages
    # on the first sync after the deploy
    op.execute(
        "UPDATE integrations SET notified_at = CURRENT_TIMESTAMP WHERE status = 'expired'"
    )
    op.create_table(
        'integration_links',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('provider', PROVIDER, nullable=False),
        sa.Column('kind', sa.Enum('food', 'entry', name='link_kind', native_enum=False, length=32), nullable=False),
        sa.Column('day', sa.Date(), nullable=True),
        sa.Column('key', sa.String(length=96), nullable=False),
        sa.Column('external_id', sa.String(length=128), nullable=False),
        sa.Column('data', JSON, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('integration_links', schema=None) as batch_op:
        batch_op.create_index('ix_integration_links_user_kind_day', ['user_id', 'provider', 'kind', 'day'], unique=False)
        batch_op.create_index('ix_integration_links_user_kind_key', ['user_id', 'provider', 'kind', 'key'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('integration_links', schema=None) as batch_op:
        batch_op.drop_index('ix_integration_links_user_kind_key')
        batch_op.drop_index('ix_integration_links_user_kind_day')
    op.drop_table('integration_links')
    with op.batch_alter_table('integrations', schema=None) as batch_op:
        batch_op.drop_column('data')
        batch_op.drop_column('notified_at')
