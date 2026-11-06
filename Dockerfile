FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --uid 10001 --create-home appuser
COPY run_job.py importar_agentes_y_tools.py importar_agentes_openpages.py importar_tools_openpages.py importar_modelos_openpages.py /app/
USER 10001
ENTRYPOINT ["python", "/app/run_job.py"]
