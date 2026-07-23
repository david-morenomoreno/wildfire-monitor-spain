FROM python:3.12-slim

WORKDIR /app

# rasterio's manylinux wheel bundles its own GDAL build, but that build still
# dynamically links a couple of libs it assumes the OS already has rather
# than bundling itself - confirmed live: python:3.12-slim is missing
# libexpat1, causing "ImportError: libexpat.so.1: cannot open shared object
# file" on `import rasterio` (see services/copernicus_ems_imagery.py).
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
