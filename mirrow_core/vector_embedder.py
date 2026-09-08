"""
向量嵌入模块
使用sentence-transformers生成文本嵌入，支持向量相似度计算和索引
"""
import numpy as np
from typing import List, Union, Optional, Tuple
import json
import os
import pickle
from dataclasses import dataclass
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

# 尝试导入sentence-transformers，如果失败则提供备用方案
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    print("警告: sentence-transformers未安装，向量嵌入功能将不可用")

# 尝试导入FAISS，如果失败则使用numpy进行线性搜索
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    print("警告: faiss未安装，将使用numpy进行向量搜索")


@dataclass
class VectorSearchResult:
    """向量搜索结果"""
    id: str
    score: float
    metadata: dict


class VectorEmbedder:
    """向量嵌入生成器"""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", cache_dir: Optional[str] = None):
        """
        初始化向量嵌入器

        Args:
            model_name: sentence-transformers模型名称
            cache_dir: 模型缓存目录
        """
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.model = None
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1)

        # 向量维度（加载模型后设置）
        self.dimension = None

        # 模型加载状态
        self._model_loaded = False

    def _ensure_model_loaded(self):
        """确保模型已加载"""
        if self._model_loaded and self.model is not None:
            return

        with self._lock:
            if self._model_loaded and self.model is not None:
                return

            if not SENTENCE_TRANSFORMERS_AVAILABLE:
                raise ImportError("sentence-transformers未安装，无法加载模型")

            try:
                print(f"正在加载向量模型: {self.model_name}")
                # 直接用本地缓存路径，huggingface_hub 的 local_files_only 在 v1.x 有 bug
                import os as _os
                cache_root = self.cache_dir or _os.path.join(_os.path.expanduser("~"), ".cache", "huggingface", "hub")
                model_dir = _os.path.join(cache_root, "models--sentence-transformers--all-MiniLM-L6-v2")
                snapshots_dir = _os.path.join(model_dir, "snapshots")
                local_path = self.model_name  # fallback
                if _os.path.isdir(snapshots_dir):
                    snapshots = sorted(_os.listdir(snapshots_dir), reverse=True)
                    for snap in snapshots:
                        snap_path = _os.path.join(snapshots_dir, snap)
                        if _os.path.isdir(snap_path) and _os.path.isfile(_os.path.join(snap_path, "model.safetensors")):
                            local_path = snap_path
                            break
                print(f"使用本地路径: {local_path}")
                # 确保离线标记在进程内生效（huggingface_hub 某些版本忽略 load_dotenv 设置）
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                self.model = SentenceTransformer(local_path)
                try:
                    self.dimension = self.model.get_embedding_dimension()
                except AttributeError:
                    self.dimension = self.model.get_sentence_embedding_dimension()
                self._model_loaded = True
                print(f"向量模型加载完成，维度: {self.dimension}")
            except Exception as e:
                print(f"加载向量模型失败: {e}")
                raise

    def embed_text(self, text: str) -> np.ndarray:
        """生成单个文本的嵌入向量"""
        self._ensure_model_loaded()
        embedding = self.model.encode(text, convert_to_numpy=True)
        return embedding.astype(np.float32)

    def embed_texts(self, texts: List[str], batch_size: int = 32) -> np.ndarray:
        """批量生成文本嵌入向量"""
        self._ensure_model_loaded()
        embeddings = self.model.encode(
            texts,
            batch_size=batch_size,
            convert_to_numpy=True,
            show_progress_bar=False
        )
        return embeddings.astype(np.float32)

    async def embed_text_async(self, text: str) -> np.ndarray:
        """异步生成单个文本的嵌入向量"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.embed_text, text)

    async def embed_texts_async(self, texts: List[str], batch_size: int = 32) -> np.ndarray:
        """异步批量生成文本嵌入向量"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self.embed_texts, texts, batch_size
        )

    @staticmethod
    def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
        """计算余弦相似度"""
        # 归一化向量
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(np.dot(vec1, vec2) / (norm1 * norm2))

    @staticmethod
    def normalize_vector(vec: np.ndarray) -> np.ndarray:
        """归一化向量"""
        norm = np.linalg.norm(vec)
        if norm == 0:
            return vec
        return vec / norm


class VectorIndex:
    """向量索引管理器，支持FAISS和numpy两种后端"""

    def __init__(self, dimension: int, use_faiss: bool = True):
        """
        初始化向量索引

        Args:
            dimension: 向量维度
            use_faiss: 是否使用FAISS（如果可用）
        """
        self.dimension = dimension
        self.use_faiss = use_faiss and FAISS_AVAILABLE

        # 向量存储
        self.vectors = []  # 存储向量列表
        self.metadata = []  # 存储元数据列表，与向量一一对应

        # FAISS索引
        self.faiss_index = None
        if self.use_faiss:
            self._init_faiss_index()

        # 锁用于线程安全
        self._lock = threading.Lock()

    def _init_faiss_index(self):
        """初始化FAISS索引"""
        try:
            # 使用内积相似度（余弦相似度需要归一化向量）
            self.faiss_index = faiss.IndexFlatIP(self.dimension)
        except Exception as e:
            print(f"初始化FAISS索引失败: {e}")
            self.use_faiss = False

    def add_vector(self, vector: np.ndarray, metadata: dict) -> int:
        """添加向量到索引"""
        with self._lock:
            idx = len(self.vectors)
            self.vectors.append(vector)
            self.metadata.append(metadata)

            if self.use_faiss and self.faiss_index is not None:
                # FAISS需要归一化向量用于内积相似度
                norm_vector = vector / np.linalg.norm(vector)
                self.faiss_index.add(norm_vector.reshape(1, -1))

            return idx

    def add_vectors(self, vectors: List[np.ndarray], metadata_list: List[dict]) -> List[int]:
        """批量添加向量到索引"""
        with self._lock:
            indices = []
            for i, (vec, meta) in enumerate(zip(vectors, metadata_list)):
                idx = len(self.vectors)
                self.vectors.append(vec)
                self.metadata.append(meta)
                indices.append(idx)

            if self.use_faiss and self.faiss_index is not None and vectors:
                # 归一化所有向量
                norm_vectors = np.array(vectors)
                norms = np.linalg.norm(norm_vectors, axis=1, keepdims=True)
                norm_vectors = norm_vectors / norms
                self.faiss_index.add(norm_vectors)

            return indices

    def remove_vector(self, metadata_id: str) -> bool:
        """从索引中移除向量（通过 metadata 中的 id 匹配）

        FAISS 不支持从 IndexFlatIP 直接删除，因此采用标记方式：
        在 metadata 中添加 _deleted=True 标记，search 时过滤。
        """
        with self._lock:
            for meta in self.metadata:
                if meta.get("id") == metadata_id:
                    meta["_deleted"] = True
                    return True
            return False

    def search(self, query_vector: np.ndarray, top_k: int = 5) -> List[VectorSearchResult]:
        """搜索相似向量"""
        if not self.vectors:
            return []

        with self._lock:
            if self.use_faiss and self.faiss_index is not None:
                # 使用FAISS搜索
                norm_query = query_vector / np.linalg.norm(query_vector)
                norm_query = norm_query.reshape(1, -1).astype(np.float32)

                # FAISS返回内积相似度
                scores, indices = self.faiss_index.search(norm_query, min(top_k, len(self.vectors)))

                results = []
                for score, idx in zip(scores[0], indices[0]):
                    if idx >= 0 and idx < len(self.metadata):
                        # 跳过已标记删除的向量
                        if self.metadata[idx].get("_deleted"):
                            continue
                        # 内积相似度转换为余弦相似度（因为向量已归一化）
                        cosine_sim = min(max(float(score), 0.0), 1.0)
                        results.append(VectorSearchResult(
                            id=self.metadata[idx].get("id", str(idx)),
                            score=cosine_sim,
                            metadata=self.metadata[idx]
                        ))
                return results
            else:
                # 使用numpy线性搜索
                return self._linear_search(query_vector, top_k)

    def _linear_search(self, query_vector: np.ndarray, top_k: int) -> List[VectorSearchResult]:
        """线性搜索相似向量"""
        similarities = []
        for i, vec in enumerate(self.vectors):
            # 跳过已标记删除的向量
            if self.metadata[i].get("_deleted"):
                continue
            sim = VectorEmbedder.cosine_similarity(query_vector, vec)
            similarities.append((sim, i))

        # 按相似度降序排序
        similarities.sort(key=lambda x: x[0], reverse=True)

        results = []
        for sim, idx in similarities[:top_k]:
            results.append(VectorSearchResult(
                id=self.metadata[idx].get("id", str(idx)),
                score=sim,
                metadata=self.metadata[idx]
            ))

        return results

    def get_vector(self, index: int) -> Optional[np.ndarray]:
        """获取指定索引的向量"""
        with self._lock:
            if 0 <= index < len(self.vectors):
                return self.vectors[index]
            return None

    def get_metadata(self, index: int) -> Optional[dict]:
        """获取指定索引的元数据"""
        with self._lock:
            if 0 <= index < len(self.metadata):
                return self.metadata[index]
            return None

    def size(self) -> int:
        """返回索引中的向量数量"""
        return len(self.vectors)

    def save(self, filepath: str):
        """保存索引到文件"""
        with self._lock:
            data = {
                "dimension": self.dimension,
                "use_faiss": self.use_faiss,
                "vectors": [vec.tolist() for vec in self.vectors],
                "metadata": self.metadata
            }

            # 如果使用FAISS且索引存在，保存FAISS索引
            if self.use_faiss and self.faiss_index is not None:
                faiss_file = filepath + ".faiss"
                faiss.write_index(self.faiss_index, faiss_file)
                data["faiss_file"] = faiss_file

            with open(filepath, 'wb') as f:
                pickle.dump(data, f)

    def load(self, filepath: str):
        """从文件加载索引"""
        with self._lock:
            with open(filepath, 'rb') as f:
                data = pickle.load(f)

            self.dimension = data["dimension"]
            self.use_faiss = data.get("use_faiss", False)
            self.vectors = [np.array(vec, dtype=np.float32) for vec in data["vectors"]]
            self.metadata = data["metadata"]

            # 加载FAISS索引
            if self.use_faiss and "faiss_file" in data and FAISS_AVAILABLE:
                try:
                    self.faiss_index = faiss.read_index(data["faiss_file"])
                except Exception as e:
                    print(f"加载FAISS索引失败: {e}")
                    self.use_faiss = False
            else:
                self._init_faiss_index()


# 全局向量嵌入器实例
_global_embedder = None


def get_global_embedder() -> VectorEmbedder:
    """获取全局向量嵌入器实例"""
    global _global_embedder
    if _global_embedder is None:
        # 用项目路径避开中文用户名导致的 [Errno 22] Invalid argument
        import os as _os
        _cache_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "model_cache")
        _global_embedder = VectorEmbedder(cache_dir=_cache_dir)
    return _global_embedder


# 测试函数
def test_vector_embedder():
    """测试向量嵌入器"""
    if not SENTENCE_TRANSFORMERS_AVAILABLE:
        print("sentence-transformers未安装，跳过测试")
        return

    embedder = VectorEmbedder()

    # 测试单个文本嵌入
    text = "这是一个测试句子"
    embedding = embedder.embed_text(text)
    print(f"文本嵌入维度: {embedding.shape}")
    print(f"嵌入向量前5维: {embedding[:5]}")

    # 测试批量嵌入
    texts = ["测试句子1", "测试句子2", "测试句子3"]
    embeddings = embedder.embed_texts(texts)
    print(f"批量嵌入形状: {embeddings.shape}")

    # 测试相似度计算
    vec1 = embedder.embed_text("我喜欢编程")
    vec2 = embedder.embed_text("编程是我的爱好")
    similarity = VectorEmbedder.cosine_similarity(vec1, vec2)
    print(f"相似度: {similarity:.4f}")

    # 测试向量索引
    index = VectorIndex(dimension=embedder.dimension)
    metadata1 = {"id": "doc1", "text": "机器学习很有趣"}
    metadata2 = {"id": "doc2", "text": "深度学习是机器学习的分支"}

    index.add_vector(vec1, metadata1)
    index.add_vector(vec2, metadata2)

    # 搜索
    query = embedder.embed_text("编程爱好")
    results = index.search(query, top_k=2)
    print(f"搜索结果: {len(results)} 条")
    for result in results:
        print(f"  ID: {result.id}, 分数: {result.score:.4f}")


if __name__ == "__main__":
    test_vector_embedder()