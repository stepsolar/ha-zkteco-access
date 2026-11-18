ARG BUILD_FROM
FROM ${BUILD_FROM}

# Solo Python standard: nessuna dipendenza esterna (niente DLL, niente Wine, niente PC)
RUN apk add --no-cache python3 tzdata

COPY app /app
WORKDIR /app

ENV PYTHONUNBUFFERED=1 ZK_DATA=/data
CMD ["python3", "-u", "/app/main.py"]
