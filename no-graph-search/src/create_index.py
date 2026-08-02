"""查詢層需要的常數（從完整的 create_index 模組摘出）。

完整的 index 建立邏輯留在主 pipeline repo，這裡只提供 search.py 引用的常數。
"""

VECTOR_FIELD = "embedding_vector"

# hybrid query 與 normalization-processor 的最低版本
MIN_HYBRID_VERSION = (2, 10)

# search pipeline ID
SEARCH_PIPELINE_ID = "jobs-hybrid-pipeline"

# 別名
INDEX_ALIAS = "jobs"
