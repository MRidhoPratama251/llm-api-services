import json
import os
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from cryptography.fernet import Fernet
import requests

# Muat variabel lingkungan dari .env jika tersedia
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ==============================================================================
# DEFAULT CREDENTIALS & URL (Disinkronisasi otomatis via .env)
# ==============================================================================
FALLBACK_API_KEY = "sk-rahasia-anda-12345"
FALLBACK_SECRET_KEY = b"2b83W7bV6D9E9sUjN-6d_7vMhU-oE1lS7a3K0gL8d9M="

env_secret = os.getenv("LLM_SECRET_KEY", "")
DEFAULT_API_KEY = os.getenv("LLM_API_KEY", FALLBACK_API_KEY)
DEFAULT_SECRET_KEY = env_secret.encode("utf-8") if env_secret else FALLBACK_SECRET_KEY

default_host = os.getenv("SERVER_HOST", "127.0.0.1")
default_port = os.getenv("SERVER_PORT", "8000")
DEFAULT_BASE_URL = os.getenv("CLIENT_TARGET_URL", f"http://{default_host}:{default_port}")


class EncryptedLLMClient:
    """
    Client SDK untuk berinteraksi dengan Encrypted LLM Middleware.
    Semua payload request & response dienkripsi secara end-to-end (E2EE)
    sehingga aman melewati tunnel publik seperti ngrok.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        secret_key: Optional[bytes] = None,
        timeout: int = 120,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or DEFAULT_API_KEY
        self.cipher = Fernet(secret_key or DEFAULT_SECRET_KEY)
        self.timeout = timeout

        self.headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "ngrok-skip-browser-warning": "true",  # Melewati layar peringatan interstitial bawaan ngrok free
        }

    def check_health(self) -> Dict[str, Any]:
        """Memeriksa status server middleware."""
        url = f"{self.base_url}/health"
        resp = requests.get(url, headers=self.headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def list_models(self) -> List[str]:
        """Mendapatkan daftar model yang tersedia."""
        url = f"{self.base_url}/v1/models"
        resp = requests.get(url, headers=self.headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("models", [])

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = "qwen-4b",
        stream: bool = False,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> Any:
        """
        Mengirim permintaan chat completion ke LLM melalui middleware terenkripsi.

        Jika stream=False: Mengembalikan Dict respons OpenAI-compatible.
        Jika stream=True: Mengembalikan Generator yang menghasilkan potongan teks (token).
        """
        payload: Dict[str, Any] = {
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
            **kwargs,
        }
        if model:
            payload["model"] = model

        # 1. Enkripsi payload di sisi client sebelum dikirim ke luar
        raw_json_str = json.dumps(payload)
        encrypted_ciphertext = self.cipher.encrypt(raw_json_str.encode("utf-8")).decode("utf-8")
        req_body = {"ciphertext": encrypted_ciphertext}

        url = f"{self.base_url}/v1/chat/completions"

        if stream:
            return self._stream_generator(url, req_body)
        else:
            return self._non_stream_request(url, req_body)

    def _non_stream_request(self, url: str, req_body: Dict[str, str]) -> Dict[str, Any]:
        """Menangani respons biasa dan mendekripsinya."""
        resp = requests.post(url, headers=self.headers, json=req_body, timeout=self.timeout)
        resp.raise_for_status()

        ciphertext = resp.json()["ciphertext"]
        decrypted_bytes = self.cipher.decrypt(ciphertext.encode("utf-8"))
        return json.loads(decrypted_bytes.decode("utf-8"))

    def _stream_generator(self, url: str, req_body: Dict[str, str]) -> Generator[str, None, None]:
        """Menangani stream SSE dari server dan mendekripsi token satu per satu."""
        resp = requests.post(
            url,
            headers=self.headers,
            json=req_body,
            stream=True,
            timeout=self.timeout,
        )
        resp.raise_for_status()

        for line in resp.iter_lines():
            if not line:
                continue

            decoded_line = line.decode("utf-8")
            if decoded_line.startswith("data: "):
                encrypted_chunk = decoded_line[6:].strip()
                if not encrypted_chunk:
                    continue

                try:
                    # 2. Dekripsi chunk SSE di sisi client
                    decrypted_raw = self.cipher.decrypt(encrypted_chunk.encode("utf-8")).decode("utf-8")
                except Exception as e:
                    yield f"\n[Client Decryption Error: {e}]\n"
                    continue

                # Cek penanda stream selesai
                if decrypted_raw.strip() == "data: [DONE]":
                    break

                # Parse JSON delta dari format SSE llama-server: "data: { ... }"
                raw_sse = decrypted_raw
                if raw_sse.startswith("data: "):
                    raw_sse = raw_sse[6:].strip()

                try:
                    chunk_json = json.loads(raw_sse)
                    choices = chunk_json.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                except json.JSONDecodeError:
                    # Jika ada raw format lain
                    pass


# ==============================================================================
# DEMO & PENGUJIAN
# ==============================================================================
if __name__ == "__main__":
    import sys

    # Ubah base_url ke alamat ngrok Anda saat menguji dari luar
    # Contoh: base_url = "https://xxxx-xx-xx.ngrok-free.app"
    default_host = os.getenv("SERVER_HOST", "127.0.0.1")
    default_port = os.getenv("SERVER_PORT", "8000")
    TARGET_URL = os.getenv("CLIENT_TARGET_URL", f"http://{default_host}:{default_port}")

    print("==================================================")
    print("      ENCRYPTED LLM CLIENT TEST SUITE             ")
    print("==================================================")
    print(f"Target URL: {TARGET_URL}\n")

    client = EncryptedLLMClient(base_url=TARGET_URL)

    # 1. Test Health
    try:
        health = client.check_health()
        print(f"[STATUS] Health Check: {health}")
    except Exception as e:
        print(f"[WARN] Health check gagal: {e}. Pastikan server main.py sudah berjalan.")
        sys.exit(1)

    # 2. Test Non-Streaming
    print("\n--- [TEST 1: NON-STREAMING COMPLETION] ---")
    prompt = "Jelaskan secara singkat dalam 2 kalimat apa itu End-to-End Encryption."
    print(f"User: {prompt}")

    try:
        response = client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model="qwen-4b",
            stream=False,
        )
        assistant_reply = response["choices"][0]["message"]["content"]
        print(f"Assistant: {assistant_reply.strip()}")
    except Exception as e:
        print(f"[ERROR] Non-streaming gagal: {e}")

    # 3. Test Streaming
    print("\n--- [TEST 2: STREAMING COMPLETION (SSE)] ---")
    prompt_stream = "Buatlah satu pantun pendek tentang keamanan siber."
    print(f"User: {prompt_stream}")
    print("Assistant (Streaming): ", end="", flush=True)

    try:
        stream_gen = client.chat_completion(
            messages=[{"role": "user", "content": prompt_stream}],
            model="qwen-4b",
            stream=True,
        )
        for token in stream_gen:
            print(token, end="", flush=True)
        print("\n")
    except Exception as e:
        print(f"\n[ERROR] Streaming gagal: {e}")

    print("--- [TEST SELESAI] ---")
