"""add researcher_id_aliases

Revision ID: 037
Revises: 036

한 사람이 연구자 ID 여러 개로 중복 등록된 것을 합치면서, 없어지는 ID → 남는 ID 대응을 남긴다.

중복은 적재 단계에서 생겼다(2026-09-29 실측, 같은 이름·같은 논문 공유 216쌍):
  - ID가 '이름 + 소속 root'의 해시라, 같은 사람의 소속 표기가 적재 회차마다 다르면 ID가 둘이 된다
    ("농촌진흥청 국립식량과학원" / "국립식량과학원")
  - ScienceON 출처(sci:)와 KCI 출처(kci:)가 병합되지 않았다
  - 8월·9월 적재의 ID 규칙이 달라 소속까지 같은 사람이 다시 만들어졌다

연구자 ID를 참조하는 사용자 데이터(북마크 등)는 없다. 그래도 공유된 링크·프런트 캐시에 옛 ID가
남아 있을 수 있어, 상세 API가 옛 ID를 받으면 이 표로 남는 ID를 찾아 응답한다.
"""
import sqlalchemy as sa
from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "researcher_id_aliases",
        sa.Column("alias_id", sa.String(100), primary_key=True),
        sa.Column(
            "researcher_id",
            sa.String(100),
            sa.ForeignKey("researchers.researcher_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_researcher_id_aliases_researcher_id", "researcher_id_aliases", ["researcher_id"])


def downgrade() -> None:
    op.drop_index("ix_researcher_id_aliases_researcher_id", table_name="researcher_id_aliases")
    op.drop_table("researcher_id_aliases")
