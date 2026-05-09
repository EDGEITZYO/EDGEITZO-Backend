import ast
import re
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions
from sentence_transformers import SentenceTransformer


MODEL_NAME = "dragonkue/BGE-m3-ko"
DISTANCE_CUTOFF = 0.70


def clean_abstract(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&amp;nbsp;", " ").replace("&nbsp;", " ").strip()


def eval_data_node(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [eval_data_node(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {
            eval_data_node(key): eval_data_node(value)
            for key, value in zip(node.keys, node.values)
        }
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "clean_abstract"
    ):
        return clean_abstract(eval_data_node(node.args[0]))
    raise TypeError(f"Unsupported node in prototype data: {ast.dump(node)[:120]}")


def load_all_papers() -> list[dict[str, str]]:
    source_path = Path(__file__).with_name("rag_prototype.py")
    module = ast.parse(source_path.read_text(encoding="utf-8"))

    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "all_papers":
                return eval_data_node(node.value)

    raise RuntimeError("all_papers data not found in rag_prototype.py")


def print_model_and_token_stats(model: SentenceTransformer, papers: list[dict[str, str]]) -> None:
    tokenizer = model.tokenizer
    max_seq_length = model.max_seq_length

    print("=" * 80)
    print("MODEL")
    print(f"name: {MODEL_NAME}")
    print(f"dimension: {model.get_sentence_embedding_dimension()}")
    print(f"max_seq_length: {max_seq_length}")
    print(f"tokenizer_model_max_length: {tokenizer.model_max_length}")
    print(f"tokenizer_class: {tokenizer.__class__.__name__}")
    print()

    rows = []
    for paper in papers:
        text = paper["abstract"]
        token_count = len(tokenizer(text, add_special_tokens=True, truncation=False)["input_ids"])
        rows.append((paper["id"], paper["title"], token_count, len(text)))

    print("TOKEN LENGTHS")
    for paper_id, title, token_count, char_count in sorted(rows, key=lambda item: item[2], reverse=True):
        print(
            f"{paper_id}: tokens={token_count}, chars={char_count}, "
            f"over_max={token_count > max_seq_length}, title={title}"
        )

    print()
    print(f"any_over_max: {any(token_count > max_seq_length for _, _, token_count, _ in rows)}")
    print(f"max_tokens: {max(token_count for _, _, token_count, _ in rows)}")
    print(f"min_tokens: {min(token_count for _, _, token_count, _ in rows)}")


def create_collection(papers: list[dict[str, str]]):
    client = chromadb.Client()
    collection = client.create_collection(
        name="papers_bge_m3_ko_tmp",
        embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=MODEL_NAME
        ),
        metadata={"hnsw:space": "cosine"},
    )

    collection.add(
        documents=[paper["abstract"] for paper in papers],
        metadatas=[{"CN": paper["CN"], "title": paper["title"]} for paper in papers],
        ids=[paper["id"] for paper in papers],
    )
    return collection


def run_queries(collection) -> None:
    test_queries = [
        "생명공학 윤리",
        "유전자 연구",
        "GM작물 안전성",
        "언론이 과학기술을 어떻게 보도하는가",
        "학생들의 생명과학 인식과 태도",
        "연구 데이터를 공유하는 방법",
        "생명공학기술의 사회적 영향과 정치적 대응",
        "교육과정에서 생명공학 내용이 어떻게 다뤄지는가",
        "인공지능 딥러닝",
        "코로나 바이러스 치료제",
    ]

    print("=" * 80)
    print("RETRIEVAL RESULTS")
    print(f"distance cutoff: {DISTANCE_CUTOFF} 미만만 관련 논문으로 처리")
    for query in test_queries:
        results = collection.query(
            query_texts=[query],
            n_results=3,
            include=["metadatas", "distances"],
        )
        print(f"질의: {query}")

        relevant_results = [
            (meta, distance)
            for meta, distance in zip(results["metadatas"][0], results["distances"][0])
            if distance < DISTANCE_CUTOFF
        ]

        if not relevant_results:
            best_distance = results["distances"][0][0]
            print(f"  관련 논문 없음 (최고 거리: {best_distance:.4f})")
            print()
            continue

        for idx, (meta, distance) in enumerate(relevant_results, start=1):
            print(f"  [{idx}위] {meta['title']} (거리: {distance:.4f})")
        print()


def main() -> None:
    papers = load_all_papers()
    model = SentenceTransformer(MODEL_NAME)
    print_model_and_token_stats(model, papers)

    # Use Chroma's embedding function separately to match the production integration path.
    collection = create_collection(papers)
    print(f"적재 완료: 총 {collection.count()}개 논문")
    run_queries(collection)


if __name__ == "__main__":
    main()
