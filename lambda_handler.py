"""AWS Lambda handler for unified search API."""

from mangum import Mangum

from unified_api.app import app

handler = Mangum(app, lifespan="off")
