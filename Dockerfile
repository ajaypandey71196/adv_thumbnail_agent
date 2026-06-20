# syntax=docker/dockerfile:1.4
# ════════════════════════════════════════════════════════════════
#  STAGE 1 — OS packages + fonts  (changes almost never → always cached)
# ════════════════════════════════════════════════════════════════
FROM python:3.10-slim-bookworm AS system-base

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgomp1 libjpeg62-turbo libfreetype6 \
        libpng16-16 libwebp7 libtiff6 libopenjp2-7 \
        libgl1 libglib2.0-0 \
        fonts-dejavu-core fonts-liberation curl fontconfig

RUN mkdir -p /usr/share/fonts/custom && \
    curl -fsSL --retry 5 --retry-delay 3 \
        -o /usr/share/fonts/custom/Montserrat-ExtraBold.ttf \
        "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf" && \
    curl -fsSL --retry 5 --retry-delay 3 \
        -o /usr/share/fonts/custom/Montserrat-Bold.ttf \
        "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Bold.ttf" && \
    fc-cache -fv


# ════════════════════════════════════════════════════════════════
#  STAGE 2a — heaviest packages + Explicit Torch-CPU (Fixes basicSR build trap)
# ════════════════════════════════════════════════════════════════
FROM system-base AS deps-heavy

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_COMPILE=1 \
    PIP_DEFAULT_TIMEOUT=1000 PIP_HTTP_TIMEOUT=1000

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install \
        --timeout 600 --retries 10 \
        "onnxruntime>=1.17" \
        "rembg>=2.0.50" \
        "opencv-python-headless>=4.8" \
        # Explicitly installing CPU wheels first so basicsr can find them instantly
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.0.0" \
        "torchvision>=0.15.0"


# ════════════════════════════════════════════════════════════════
#  STAGE 2b — AI enhancer packages (GFPGAN + Real-ESRGAN)
# ════════════════════════════════════════════════════════════════
FROM deps-heavy AS deps-enhancers

# We temporarily add /install to PATH and PYTHONPATH so setuptools 
# can verify torch installation during setup.py metadata execution.
ENV PATH="/install/bin:$PATH" \
    PYTHONPATH="/install/lib/python3.10/site-packages:$PYTHONPATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_COMPILE=1

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install \
        --timeout 300 --retries 5 \
        "facexlib>=0.3.0" \
        "gfpgan>=1.3.8" \
        "realesrgan>=0.3.0" \
        # basicsr needs to be built with PEP 517 isolation disabled sometimes, 
        # or it just seamlessly works if torch is in the environment path.
        "basicsr>=1.4.2"


# ════════════════════════════════════════════════════════════════
#  STAGE 2c — lightweight app deps (fast, ~30 MB, changes often)
# ════════════════════════════════════════════════════════════════
FROM deps-enhancers AS deps-app

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install \
        --timeout 120 --retries 3 \
        "groq>=0.9.0" \
        "httpx==0.27.2" \
        "google-api-python-client" \
        "google-auth-httplib2" \
        "google-auth-oauthlib" \
        "pillow" \
        "requests" \
        "urllib3"


# ════════════════════════════════════════════════════════════════
#  STAGE 3 — runtime  (code-only layer, rebuilds in ~2 seconds)
# ════════════════════════════════════════════════════════════════
FROM system-base AS runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY --from=deps-app /install/lib/python3.10/site-packages \
                     /usr/local/lib/python3.10/site-packages
COPY --from=deps-app /install/bin /usr/local/bin

# Pre-download rembg ONNX model so first run isn't slow
RUN mkdir -p /root/.u2net && \
    curl -fsSL --retry 5 --retry-delay 3 \
        -o /root/.u2net/u2net.onnx \
        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"

WORKDIR /app

COPY advanced_pipeline.py ./
COPY assets/ ./assets/

CMD ["python", "-u", "advanced_pipeline.py"]
