"""add paper_citation_external_refs.paper_type / published_at

Revision ID: 036
Revises: 035

인용관계 그래프의 코퍼스 밖 노드에 **논문 유형**과 **발행일**을 붙인다.
지금은 pubyear(연도 정수)만 있어서 "2007년"까지만 말할 수 있었다.

발행일을 DATE가 아니라 가변 길이 문자열로 두는 이유:
  Crossref issued.date-parts의 정밀도가 원본마다 다르다. 표본 45건 실측(2026-09-24):
    연-월-일  21건 (46.7%)
    연-월     22건 (48.9%)
    연도만     1건 ( 2.2%)
    없음       1건 ( 2.2%)
  학술지가 "2007년 4월호"로 내고 일자를 안 밝히는 게 흔해서 연-월이 절반이다. 원본에
  일자가 없는 것이지 Crossref가 누락한 게 아니다. DATE로 받으면 그 48.9%에 01을 채워
  넣게 되는데, "4월 1일 발행"은 사실이 아니라 화면에 나가면 틀린 정보가 된다.
  그래서 있는 만큼만 넣는다 — "2007-04-15" / "2007-04" / "2007" / null.
  사전순 정렬이 곧 시간순이고 LIKE '2007%'도 자연스럽다. papers.pubdate도 같은 이유로
  character varying이다.

  연·월·일을 칸 세 개로 나누는 방식은 택하지 않았다. 정렬·비교할 때마다 세 칸을
  조합해야 하고, 화면에 뿌릴 때도 매번 이어붙여야 한다.

paper_type은 Crossref type을 그대로 쓴다(표본에서 journal-article 95.6%).
OpenAlex type은 Crossref가 없을 때의 폴백이다.
"""
import sqlalchemy as sa
from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 기본값 없는 ADD COLUMN이라 PG11+에서는 메타데이터만 바뀌어 즉시 끝난다.
    # 오래 걸리는 건 실행이 아니라 **락을 잡는 순간**이다 — 2026-09-18에 11일 방치된
    # idle in transaction 세션 때문에 ALTER TABLE users가 무한 대기하면서 users 조회가
    # 통째로 정체된 적이 있다. 배포는 ssh-action 10분 타임아웃이라 그대로 끊긴다.
    # 락을 5초 안에 못 잡으면 대기 대신 실패시켜, 배포가 중간에 끊기는 대신
    # 명확한 에러로 끝나게 한다.
    op.execute("SET lock_timeout = '5s'")
    op.add_column(
        "paper_citation_external_refs",
        sa.Column("paper_type", sa.String(50), nullable=True),
    )
    op.add_column(
        "paper_citation_external_refs",
        sa.Column("published_at", sa.String(10), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_column("paper_citation_external_refs", "published_at")
    op.drop_column("paper_citation_external_refs", "paper_type")
