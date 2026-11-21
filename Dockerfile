# Same base image the organizers labeled in. The selector itself only needs
# Python's standard library: no tests are run at decision time.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py /app/selector.py
COPY maps /app/maps
RUN chmod -R a+rX /app && python -m compileall -q /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "/app/selector.py"]
