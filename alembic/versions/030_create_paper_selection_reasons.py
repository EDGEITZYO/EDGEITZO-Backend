"""논문 선정 사유 영속 캐시

Revision ID: 030
Revises: 029
Create Date: 2026-08-27

명세 02-11 「논문 선정 이유」— 검색 결과 카드에서 초록 대신 "AI가 왜 이 논문을 골랐는지"를
보여준다. 초록과 달리 검색어에 종속되는 값이라 논문마다 미리 만들어둘 수 없고, 매번
LLM을 호출하면 비용이 감당되지 않는다(코퍼스 1,000편 × 검색 키워드 조합).

Redis 캐시(24시간)로는 다음 날이면 다시 과금되므로 Postgres에 영속 저장한다.
같은 패턴이 keyword_definitions에 이미 있다 — "최초 조회 시 생성해 영속 저장".

PK가 (paper_id, keyword_key, prompt_version)인 이유:
- keyword_key  : 검색 키워드를 정규화·정렬해 이어붙인 값. 필터/정렬을 바꿔도 키워드가
                 그대로면 재사용된다(필터는 결과를 좁힐 뿐 사유를 바꾸지 않음).
- prompt_version: 프롬프트 규칙을 고치면 기존 행은 전부 구 버전 글이 된다. 버전을 키에
                 넣어야 재생성되고, 옛 버전은 남겨뒀다가 검증 후 지울 수 있다.

paper_id에 FK를 걸지 않는다 — 단순 검색/자연어 검색 모두 Chroma가 돌려준 CN을 쓰는데
papers 테이블에 없는 ID가 섞일 수 있고(적재 시점 차이), 그때 캐시 저장이 실패하면
사유가 통째로 사라진다. 캐시는 본체가 아니므로 느슨하게 둔다.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_selection_reasons",
        sa.Column("paper_id", sa.String(length=100), nullable=False),
        sa.Column("keyword_key", sa.String(length=500), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # 강조 구절의 reason 내 위치. LLM이 마커를 안 넣거나 규칙을 어기면 null이 되고,
        # 그때 프런트는 하이라이트 없이 본문만 그린다(본문 자체는 항상 정상).
        sa.Column("highlight_start", sa.Integer(), nullable=True),
        sa.Column("highlight_end", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("paper_id", "keyword_key", "prompt_version"),
    )
    # 프롬프트 버전을 올린 뒤 구 버전 행을 일괄 삭제할 때 쓴다.
    op.create_index(
        "ix_paper_selection_reasons_version",
        "paper_selection_reasons",
        ["prompt_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_paper_selection_reasons_version", table_name="paper_selection_reasons")
    op.drop_table("paper_selection_reasons")
