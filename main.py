import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
import httpx

# Muat variabel lingkungan dari .env jika tersedia
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

from services import LocalModelManager

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("LLM-Middleware")

# ==============================================================================
# KONFIGURASI KEAMANAN & LINGKUNGAN (.env)
# ==============================================================================
DEFAULT_API_KEY = "sk-rahasia-anda-12345"
DEFAULT_SECRET_KEY = b"2b83W7bV6D9E9sUjN-6d_7vMhU-oE1lS7a3K0gL8d9M="

SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

API_KEY = os.getenv("LLM_API_KEY", DEFAULT_API_KEY)
secret_key_env = os.getenv("LLM_SECRET_KEY", "")
SECRET_KEY = secret_key_env.encode("utf-8") if secret_key_env else DEFAULT_SECRET_KEY

try:
    cipher = Fernet(SECRET_KEY)
except Exception as e:
    logger.error(f"Kunci enkripsi tidak valid: {e}")
    sys.exit(1)

# Inisialisasi Model Manager dari services.py
manager = LocalModelManager(
    config_path=os.getenv("MODEL_CONFIG_PATH", "model_config.json"),
    base_url=os.getenv("LLAMA_SERVER_BASE_URL", "http://localhost:8080"),
    ready_timeout=int(os.getenv("READY_TIMEOUT", "30")),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager untuk memastikan proses LLM dibersihkan saat server ditutup."""
    logger.info("Middleware dimulai. Menyiapkan layanan...")
    yield
    logger.info("Middleware mematikan server. Membersihkan proses LLM dan alokasi VRAM...")
    manager.matikan_model_aktif()


app = FastAPI(
    title="Encrypted LLM Proxy Middleware",
    description="Middleware Zero-Trust dengan End-to-End Encryption untuk akses LLM via ngrok.",
    version="1.0.0",
    lifespan=lifespan,
)


# ==============================================================================
# PYDANTIC SCHEMAS
# ==============================================================================
class EncryptedPayload(BaseModel):
    ciphertext: str = Field(
        ...,
        description="String ciphertext hasil enkripsi Fernet dari objek JSON request.",
        examples=["gAAAAABl..."],
    )


# ==============================================================================
# HELPER & AUTHENTICATION
# ==============================================================================
def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    """Memverifikasi bahwa request menyertakan API Key yang valid."""
    if x_api_key != API_KEY:
        logger.warning("Percobaan akses dengan API Key tidak valid.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: API Key tidak valid atau tidak disertakan.",
        )


# ==============================================================================
# ROUTES
# ==============================================================================
@app.get("/health")
def health_check():
    """Pemeriksaan status server middleware dan status proses model saat ini."""
    return {
        "status": "online",
        "current_model": manager.current_model,
        "is_model_running": manager.current_process is not None,
    }


@app.get("/v1/models")
def list_models(x_api_key: str = Header(..., alias="X-API-Key")):
    """Mendapatkan daftar model yang terdaftar pada konfigurasi."""
    verify_api_key(x_api_key)
    models = list(manager.model_configs.keys())
    return {
        "models": models,
        "current_model": manager.current_model,
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    payload: EncryptedPayload,
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """
    Endpoint utama inferensi LLM:
    - Menerima payload terenkripsi (E2EE)
    - Mendekripsi payload secara lokal di memori server
    - Menangani peralihan model jika model yang diminta berbeda
    - Meneruskan ke llama-server lokal
    - Mengenkripsi kembali respon (Streaming SSE atau JSON tunggal)
    """
    verify_api_key(x_api_key)

    # 1. Dekripsi data request
    try:
        decrypted_raw = cipher.decrypt(payload.ciphertext.encode("utf-8")).decode("utf-8")
        request_data = json.loads(decrypted_raw)
    except (InvalidToken, json.JSONDecodeError) as e:
        logger.error(f"Gagal mendekripsi atau mem-parsing payload: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload korup atau gagal didekripsi dengan kunci yang sesuai.",
        )

    model_name = request_data.get("model")
    is_stream = request_data.get("stream", False)

    # 2. Otomasi pergantian model jika diminta
    if model_name:
        if model_name != manager.current_model:
            logger.info(f"Permintaan beralih ke model [{model_name}]. Memulai peralihan...")
            ready = manager.ganti_model(model_name)
            if not ready:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Gagal menginisialisasi model [{model_name}].",
                )
    elif manager.current_model is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tidak ada model yang aktif. Tentukan parameter 'model' dalam request.",
        )

    # 3. Mode Streaming vs Non-Streaming
    if is_stream:
        return handle_streaming_request(request_data)
    else:
        return await handle_non_streaming_request(request_data)


def handle_streaming_request(request_data: dict) -> StreamingResponse:
    """Menangani request SSE streaming dengan mengenkripsi tiap baris data secara on-the-fly."""
    async def event_generator() -> AsyncGenerator[str, None]:
        timeout = httpx.Timeout(180.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    manager.completion_url,
                    json=request_data,
                ) as upstream_response:
                    if upstream_response.status_code != 200:
                        error_body = await upstream_response.aread()
                        logger.error(f"Upstream error {upstream_response.status_code}: {error_body.decode('utf-8', errors='ignore')}")
                        error_msg = json.dumps({"error": f"Upstream llama-server error {upstream_response.status_code}"})
                        encrypted_err = cipher.encrypt(error_msg.encode("utf-8")).decode("utf-8")
                        yield f"data: {encrypted_err}\n\n"
                        return

                    async for line in upstream_response.aiter_lines():
                        if not line:
                            continue
                        
                        # Baris dari llama-server biasanya: "data: {...}" atau "data: [DONE]"
                        # Enkripsi baris data sebelum dikirimkan ke tunnel ngrok
                        encrypted_chunk = cipher.encrypt(line.encode("utf-8")).decode("utf-8")
                        yield f"data: {encrypted_chunk}\n\n"

            except httpx.RequestError as exc:
                logger.error(f"Koneksi streaming ke llama-server terputus: {exc}")
                error_msg = json.dumps({"error": f"Upstream connection failed: {str(exc)}"})
                encrypted_err = cipher.encrypt(error_msg.encode("utf-8")).decode("utf-8")
                yield f"data: {encrypted_err}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_non_streaming_request(request_data: dict) -> JSONResponse:
    """Menangani request biasa (non-streaming) dan mengembalikan ciphertext terenkripsi penuh."""
    timeout = httpx.Timeout(180.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(manager.completion_url, json=request_data)
            if resp.status_code != 200:
                logger.error(f"Upstream status {resp.status_code}: {resp.text}")
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Upstream LLM error: {resp.text}",
                )

            # Enkripsi seluruh respon JSON lokal sebelum dikembalikan melalui ngrok
            encrypted_content = cipher.encrypt(resp.content).decode("utf-8")
            return JSONResponse(content={"ciphertext": encrypted_content})

        except httpx.RequestError as exc:
            logger.error(f"Gagal menghubungi llama-server: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Gagal berkomunikasi dengan llama-server lokal: {str(exc)}",
            )


if __name__ == "__main__":
    import uvicorn
    # Menjalankan server menggunakan host & port dari konfigurasi (.env)
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
