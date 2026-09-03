"""users: bring-your-own Anthropic key (llm_key_enc, llm_key_last4, llm_key_set_at)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03 18:30:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('llm_key_enc', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('llm_key_last4', sa.String(length=4), nullable=True))
        batch_op.add_column(sa.Column('llm_key_set_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('llm_key_set_at')
        batch_op.drop_column('llm_key_last4')
        batch_op.drop_column('llm_key_enc')
