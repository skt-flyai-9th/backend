"""drop unique constraint on video_formats reference_url

Revision ID: eef08bf1ec77
Revises: e96ee07addd2
Create Date: 2026-08-28 14:09:41.256338

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eef08bf1ec77"
down_revision: Union[str, Sequence[str], None] = "e96ee07addd2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f("reference_url"), table_name="video_formats")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index(op.f("reference_url"), "video_formats", ["reference_url"], unique=True)
