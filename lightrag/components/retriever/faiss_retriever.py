"""Local semantic search/embedding-based retriever using FAISS."""

from typing import List, Optional, Sequence, Union, Dict, overload
import numpy as np
import logging
import os

try:
    import faiss
except ImportError:
    raise ImportError("Please install faiss with: pip install faiss")


from lightrag.core.retriever import Retriever
from lightrag.core.embedder import Embedder
from lightrag.core.types import (
    RetrieverOutput,
    RetrieverOutputType,
    EmbedderOutputType,
)

log = logging.getLogger(__name__)

EmbeddingType = Union[List[float], np.ndarray]  # single embedding
FAISSRetrieverDocumentType = Sequence[
    EmbeddingType
]  # embeddings, #TODO: directly use numpy array as embeddings in the whole library

FAISSRetrieverStringInputType = Union[str, List[str]]  # single query or list of queries
FAISSRetrieverEmbeddingInputType = Union[
    List[float], List[List[float]], np.ndarray
]  # single embedding or list of embeddings

FAISSRetrieverInputType = Union[
    FAISSRetrieverEmbeddingInputType, FAISSRetrieverStringInputType
]

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"


class FAISSRetriever(Retriever):
    __doc__ = r"""Local semantic search/embedding-based retriever using FAISS.

    To use the retriever, ensure to call :meth:`build_index_from_documents` before calling :meth:`retrieve`.

    Args:
        embedder (Embedder, optimal): The embedder component to use for converting the queries in string format to embeddings.
            Ensure the vectorizer is exactly the same as the one used to the embeddings in the index.
        top_k (int, optional): Number of chunks to retrieve. Defaults to 5.
        dimensions (Optional[int], optional): Dimension of the embeddings. Defaults to None. It can automatically infer the dimensions from the first chunk.
    
    How FAISS works:

    The retriever uses in-memory Faiss index to retrieve the top k chunks
    d: dimension of the vectors
    xb: number of vectors to put in the index
    xq: number of queries
    The data type dtype must be float32.

    Note: When the num of chunks are less than top_k, the last columns will be -1

    Other index options:
    - faiss.IndexFlatL2: L2 or Euclidean distance, [-inf, inf]
    - faiss.IndexFlatIP: inner product of normalized vectors will be cosine similarity, [-1, 1]

    We choose cosine similarity and convert it to range [0, 1] by adding 1 and dividing by 2 to simulate probability in [0, 1]

    References:
    - FAISS: https://github.com/facebookresearch/faiss
    """

    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        top_k: int = 5,
        dimensions: Optional[int] = None,
        documents: Optional[FAISSRetrieverDocumentType] = None,
    ):
        super().__init__()
        self.dimensions = dimensions
        self.embedder = embedder  # used to vectorize the queries
        self.top_k = top_k

        self.index = None
        if documents:
            self.build_index_from_documents(documents)

    def reset_index(self):
        self.index.reset() if self.index else None
        self.total_chunks: int = 0
        self.indexed = False

    def build_index_from_documents(
        self,
        documents: FAISSRetrieverDocumentType,
    ):
        r"""Build index from embeddings.

        Args:
            documents: List of embeddings. Format can be List[List[float]] or List[np.ndarray]

        If you are using Document format, pass them as [doc.vector for doc in documents]
        """
        try:
            self.total_chunks = len(documents)

            # convert to numpy array
            if not isinstance(documents, np.ndarray) and isinstance(
                documents[0], Sequence
            ):
                # ensure all the embeddings are of the same size
                assert all(
                    len(doc) == len(documents[0]) for doc in documents
                ), "All embeddings should be of the same size"
                xb = np.array(documents, dtype=np.float32)
            else:
                xb = documents
            # check dimensions
            if not self.dimensions:
                self.dimensions = xb.shape[1]
            else:
                assert (
                    self.dimensions == xb.shape[1]
                ), f"Dimension mismatch: {self.dimensions} != {xb.shape[1]}"
            # prepare the faiss index
            self.index = faiss.IndexFlatIP(self.dimensions)
            self.index.add(xb)
            self.indexed = True
            log.info(f"Index built with {self.total_chunks} chunks")
        except Exception as e:
            log.error(f"Error building index: {e}, resetting the index")
            # reset the index
            self.reset_index()
            raise e

    def _convert_cosine_similarity_to_probability(self, D: np.ndarray) -> np.ndarray:
        D = (D + 1) / 2
        D = np.round(D, 3)
        return D

    def _to_retriever_output(
        self, Ind: np.ndarray, D: np.ndarray
    ) -> RetrieverOutputType:
        r"""Convert the indices and distances to RetrieverOutputType format."""
        output: RetrieverOutputType = []
        # Step 1: Filter out the -1, -1 columns along with its scores when top_k > len(chunks)
        if -1 in Ind:
            valid_columns = ~np.any(Ind == -1, axis=0)

            D = D[:, valid_columns]
            Ind = Ind[:, valid_columns]
        # Step 2: processing rows (one query at a time)
        for row in zip(Ind, D):
            indices, distances = row
            # chunks: List[Chunk] = []
            retrieved_documents_indices = indices
            retrieved_documents_scores = distances
            output.append(
                RetrieverOutput(
                    doc_indices=retrieved_documents_indices,
                    doc_scores=retrieved_documents_scores,
                )
            )

        return output

    def retrieve_embedding_queries(
        self,
        input: FAISSRetrieverEmbeddingInputType,
        top_k: Optional[int] = None,
    ) -> RetrieverOutputType:
        # check if the input is List, convert to numpy array
        try:
            if not isinstance(input, np.ndarray):
                xq = np.array(input, dtype=np.float32)
        except Exception as e:
            log.error(f"Error converting input to numpy array: {e}")
            raise e

        D, Ind = self.index.search(xq, top_k if top_k else self.top_k)
        D = self._convert_cosine_similarity_to_probability(D)
        output: RetrieverOutputType = self._to_retriever_output(Ind, D)
        return output

    def retrieve_string_queries(
        self,
        input: Union[str, List[str]],
        top_k: Optional[int] = None,
    ) -> RetrieverOutputType:
        r"""Retrieve the top k chunks given the query or queries in string format.

        Args:
            input: The query or list of queries in string format. Note: ensure the maximum number of queries fits into the embedder.
            top_k: The number of chunks to retrieve. When top_k is not provided, it will use the default top_k set during initialization.

        When top_k is not provided, it will use the default top_k set during initialization.
        """
        if self.index.ntotal == 0:
            raise ValueError(
                "Index is empty. Please set the chunks to build the index from"
            )
        queries = [input] if isinstance(input, str) else input
        # filter out empty queries
        valid_queries: List[str] = []
        record_map: Dict[int, int] = (
            {}
        )  # final index : the position in the initial queries
        for i, q in enumerate(queries):
            if not q:
                log.warning("Empty query found, skipping")
                continue
            valid_queries.append(q)
            record_map[len(valid_queries) - 1] = i
        # embed the queries, assume the length fits into a batch.
        try:
            embeddings: EmbedderOutputType = self.embedder(valid_queries)
            queries_embeddings: List[float] = [
                data.embedding for data in embeddings.data
            ]
        except Exception as e:
            log.error(f"Error embedding queries: {e}")
            raise e
        xq = np.array(queries_embeddings, dtype=np.float32)
        D, Ind = self.index.search(xq, top_k if top_k else self.top_k)
        D = self._convert_cosine_similarity_to_probability(D)

        output: RetrieverOutputType = [
            RetrieverOutput(doc_indices=[], query=query) for query in queries
        ]
        retrieved_output: RetrieverOutputType = self._to_retriever_output(Ind, D)

        # fill in the doc_indices and score for valid queries
        for i, per_query_output in enumerate(retrieved_output):
            initial_index = record_map[i]
            output[initial_index].doc_indices = per_query_output.doc_indices
            output[initial_index].doc_scores = per_query_output.doc_scores

        return output

    @overload
    def __call__(
        self,
        input: FAISSRetrieverEmbeddingInputType,
        top_k: Optional[int] = None,
    ) -> RetrieverOutputType: ...

    r"""Retrieve the top k chunks given the query or queries in embedding format."""

    @overload
    def __call__(
        self,
        input: FAISSRetrieverStringInputType,
        top_k: Optional[int] = None,
    ) -> RetrieverOutputType: ...

    r"""Retrieve the top k chunks given the query or queries in string format."""

    def __call__(
        self,
        input: Union[FAISSRetrieverEmbeddingInputType, FAISSRetrieverStringInputType],
        top_k: Optional[int] = None,
    ) -> RetrieverOutputType:
        r"""Retrieve the top k chunks given the query or queries in embedding or string format."""
        assert (
            self.indexed
        ), "Index is not built. Please build the index using build_index_from_documents"
        if isinstance(input, str) or (
            isinstance(input, Sequence) and isinstance(input[0], str)
        ):
            return self.retrieve_string_queries(input, top_k)
        else:
            return self.retrieve_embedding_queries(input, top_k)

    def _extra_repr(self) -> str:
        s = f"top_k={self.top_k}"
        if self.dimensions:
            s += f", dimensions={self.dimensions}"
        return s
