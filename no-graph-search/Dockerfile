FROM public.ecr.aws/lambda/python:3.13

# Install only runtime dependencies (skip pandas/numpy/pyarrow for Lambda)
COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt

# Copy application code
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY config/ ${LAMBDA_TASK_ROOT}/config/
COPY cache/ ${LAMBDA_TASK_ROOT}/cache/

# Handler
CMD ["src.lambda_handler.handler"]
