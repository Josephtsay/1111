"""AWS Lambda handler — 用 Mangum 把 FastAPI app 適配成 Lambda 處理函式。

部署到 Lambda 時，handler 設定為 src.lambda_handler.handler。
API Gateway 以 HTTP API (v2) 或 REST API 都支援。
"""

from mangum import Mangum

from src.api import app

handler = Mangum(app, lifespan="off")
