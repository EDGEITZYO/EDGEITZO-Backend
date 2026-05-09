import chromadb
from sentence_transformers import SentenceTransformer


papers = [
    {
        "paper_id": "JAKO200405812524620",
        "title": "한국신문의 유전자 연구 프레임 비교 분석",
        "abstract": "본 논문은 한국 언론이 유전자 연구에 대해 어떻게 현실을 구성하고 있는지를 신문 프레임 연구 방법을 통해 분석한다. 유전자 연구의 윤리적, 경제적 이슈와 사회적 갈등 해결 방안을 다룬다.",
    },
    {
        "paper_id": "JAKO202414433385011",
        "title": "생명공학기술 관련 학습 내용 분석",
        "abstract": "본 연구는 생명과학II와 공학일반 교과서에 제시된 생명공학기술 관련 학습 내용을 분석하고, 생명공학기술 교육을 위한 시사점을 제공한다.",
    },
    {
        "paper_id": "JAKO200230758883726",
        "title": "생명공학에 대한 한국인들의 표상",
        "abstract": "본 연구는 생명공학과 관련 기술의 활용에 대한 일반 국민들의 인식과 태도, 그리고 그에 영향을 미치는 심리적 요인을 분석하였다.",
    },
]


model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

client = chromadb.Client()
collection = client.get_or_create_collection(name="paper_abstracts")

for paper in papers:
    text = paper["title"] + "\n" + paper["abstract"]
    embedding = model.encode(text).tolist()

    collection.add(
        ids=[paper["paper_id"]],
        embeddings=[embedding],
        documents=[text],
        metadatas=[{"title": paper["title"]}],
    )

queries = [
    "생명공학 교육 관련 논문",
    "유전자 연구 윤리 문제",
    "생명공학에 대한 사회적 인식",
]

for query in queries:
    query_embedding = model.encode(query).tolist()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=2,
    )

    print("=" * 70)
    print("질문:", query)

    for title, doc_id in zip(results["metadatas"][0], results["ids"][0]):
        print("검색 결과:", title["title"])
        print("paper_id:", doc_id)


