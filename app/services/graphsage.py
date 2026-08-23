"""비지도 GraphSAGE — 공저 네트워크를 반영한 연구자 임베딩.

Hamilton et al. (2017) "Inductive Representation Learning on Large Graphs"의
비지도 목적함수를 그대로 쓴다. 정답 라벨이 필요 없다:

    이웃인 두 노드는 벡터를 가깝게, 무작위로 뽑은 남남은 멀게

        L = -log σ(z_u · z_v) - Q · E_{n~Pn} log σ(-z_u · z_n)

Neo4j GDS에도 GraphSAGE가 있지만 Aura에서는 별도 유료 세션(gds.session.getOrCreate)이
필요해, 비용 없이 재현 가능하도록 직접 구현했다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class TrainConfig:
    hidden_dim: int = 256
    output_dim: int = 128
    sample_sizes: tuple[int, int] = (25, 10)  # 1홉 25명, 2홉 10명까지만 표본 추출
    epochs: int = 30
    batch_size: int = 512
    learning_rate: float = 0.005
    negative_samples: int = 5  # 논문의 Q
    seed: int = 42


class MeanAggregatorLayer(nn.Module):
    """자기 자신과 이웃 평균을 이어붙여 한 번 변환하는 층(GraphSAGE-mean)."""

    def __init__(self, in_dim: int, out_dim: int):
        super().__init__()
        self.linear = nn.Linear(in_dim * 2, out_dim)

    def forward(self, self_feats: torch.Tensor, neighbour_feats: torch.Tensor) -> torch.Tensor:
        return self.linear(torch.cat([self_feats, neighbour_feats], dim=1))


class GraphSAGE(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int, out_dim: int):
        super().__init__()
        self.layer1 = MeanAggregatorLayer(in_dim, hidden_dim)
        self.layer2 = MeanAggregatorLayer(hidden_dim, out_dim)

    def forward(self, x_self, x_hop1, x_hop1_hop2) -> torch.Tensor:
        """x_self (B,F) / x_hop1 (B,S1,F) / x_hop1_hop2 (B,S1,S2,F)."""
        batch, s1, _feat = x_hop1.shape

        # 1홉 이웃들을 각자의 2홉 이웃 평균과 합쳐 한 번 변환한다.
        hop1_neighbour_mean = x_hop1_hop2.mean(dim=2)                    # (B,S1,F)
        hop1_hidden = F.relu(
            self.layer1(x_hop1.reshape(batch * s1, -1), hop1_neighbour_mean.reshape(batch * s1, -1))
        ).reshape(batch, s1, -1)                                          # (B,S1,H)

        # 중심 노드도 같은 층으로 변환하되, 이웃 평균은 1홉 원본 피처를 쓴다.
        self_hidden = F.relu(self.layer1(x_self, x_hop1.mean(dim=1)))     # (B,H)

        out = self.layer2(self_hidden, hop1_hidden.mean(dim=1))           # (B,O)
        return F.normalize(out, p=2, dim=1)


class NeighbourSampler:
    """CSR 형태의 인접 리스트에서 고정 개수의 이웃을 복원추출한다.

    이웃이 없는 노드(고립 367개 요소 중 다수)는 자기 자신을 이웃으로 삼는다 —
    그러면 GraphSAGE가 사실상 피처만 쓰는 MLP처럼 동작해 임베딩이 비지 않는다.
    """

    def __init__(self, num_nodes: int, edges: np.ndarray, rng: np.random.Generator):
        self.rng = rng
        both = np.concatenate([edges, edges[:, ::-1]], axis=0)
        order = np.argsort(both[:, 0], kind="stable")
        both = both[order]
        self.targets = both[:, 1]
        self.offsets = np.searchsorted(both[:, 0], np.arange(num_nodes + 1))

    def sample(self, nodes: np.ndarray, size: int) -> np.ndarray:
        out = np.empty((len(nodes), size), dtype=np.int64)
        for i, node in enumerate(nodes):
            start, end = self.offsets[node], self.offsets[node + 1]
            if end > start:
                out[i] = self.rng.choice(self.targets[start:end], size=size, replace=True)
            else:
                out[i] = node
        return out


def train_unsupervised(
    features: np.ndarray,
    edges: np.ndarray,
    config: TrainConfig | None = None,
    *,
    device: str = "cpu",
    log=print,
) -> np.ndarray:
    """features (N,F), edges (E,2) → 그래프 임베딩 (N,output_dim)."""
    config = config or TrainConfig()
    torch.manual_seed(config.seed)
    rng = np.random.default_rng(config.seed)

    num_nodes, in_dim = features.shape
    x = torch.tensor(features, dtype=torch.float32, device=device)
    sampler = NeighbourSampler(num_nodes, edges, rng)
    model = GraphSAGE(in_dim, config.hidden_dim, config.output_dim).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
    s1, s2 = config.sample_sizes

    def embed(nodes: np.ndarray) -> torch.Tensor:
        hop1 = sampler.sample(nodes, s1)                       # (B,S1)
        hop2 = sampler.sample(hop1.reshape(-1), s2)            # (B*S1,S2)
        return model(
            x[torch.as_tensor(nodes, device=device)],
            x[torch.as_tensor(hop1, device=device)],
            x[torch.as_tensor(hop2.reshape(len(nodes), s1, s2), device=device)],
        )

    for epoch in range(1, config.epochs + 1):
        model.train()
        shuffled = edges[rng.permutation(len(edges))]
        total_loss, batches = 0.0, 0
        for start in range(0, len(shuffled), config.batch_size):
            batch = shuffled[start : start + config.batch_size]
            if len(batch) < 2:
                continue
            z_u, z_v = embed(batch[:, 0]), embed(batch[:, 1])
            negatives = rng.integers(0, num_nodes, size=(len(batch), config.negative_samples))
            z_neg = embed(negatives.reshape(-1)).reshape(len(batch), config.negative_samples, -1)

            positive = F.logsigmoid((z_u * z_v).sum(dim=1))
            negative = F.logsigmoid(-(z_neg @ z_u.unsqueeze(2)).squeeze(2)).sum(dim=1)
            loss = -(positive + negative).mean()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.detach().item()
            batches += 1
        if epoch == 1 or epoch % 5 == 0 or epoch == config.epochs:
            log(f"  epoch {epoch:>3}/{config.epochs}  loss {total_loss / max(batches, 1):.4f}")

    model.eval()
    outputs = []
    with torch.no_grad():
        for start in range(0, num_nodes, 1024):
            outputs.append(embed(np.arange(start, min(start + 1024, num_nodes))).cpu().numpy())
    return np.concatenate(outputs, axis=0)
