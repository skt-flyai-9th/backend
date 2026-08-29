"""add estimated_shooting_sec to video_formats

Revision ID: 3e1cb8f79a8a
Revises: eef08bf1ec77
Create Date: 2026-08-30 00:02:31.528523

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = '3e1cb8f79a8a'
down_revision: Union[str, Sequence[str], None] = 'eef08bf1ec77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('video_formats', sa.Column('estimated_shooting_sec', sa.Integer(), nullable=True, comment='예상 촬영 소요시간(초). 템플릿 고정값, AI 조회 캐시'))
    op.alter_column('video_formats', 'expected_duration_sec',
               existing_type=mysql.INTEGER(),
               comment='완성 영상 길이(초). API 응답 필드명은 reference_duration_sec',
               existing_comment='예상 촬영/영상 시간(초)',
               existing_nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('video_formats', 'expected_duration_sec',
               existing_type=mysql.INTEGER(),
               comment='예상 촬영/영상 시간(초)',
               existing_comment='완성 영상 길이(초). API 응답 필드명은 reference_duration_sec',
               existing_nullable=True)
    op.drop_column('video_formats', 'estimated_shooting_sec')
