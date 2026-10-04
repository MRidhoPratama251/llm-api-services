import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

# ==================
# Class untuk mengelola model LLM local dengan llama-server
# ==================
class LocalModelManager:
    def __init__(
        self,
        config_path: Optional[str] = None,
        base_url: str = "http://localhost:8080",
        ready_timeout: int = 30,
    ) -> None:
        if config_path is None:
            self.config_path = Path(__file__).parent / "model_config.json"
        else:
            self.config_path = Path(config_path)
            if not self.config_path.exists():
                fallback = Path(__file__).parent / config_path
                if fallback.exists():
                    self.config_path = fallback

        self.base_url = base_url.rstrip("/")
        self.ready_timeout = ready_timeout

        self.current_process: Optional[subprocess.Popen] = None
        self.current_model: Optional[str] = None
        self.model_configs: Dict[str, List[str]] = {}

        self.muat_konfigurasi()

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    @property
    def completion_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    def muat_konfigurasi(self) -> None:
        """Memuat pemetaan perintah CLI model dari file JSON."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"[ERROR] File konfigurasi tidak ditemukan di: {self.config_path.resolve()}"
            )

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.model_configs = json.load(f)
            print(f"[INFO] Berhasil memuat {len(self.model_configs)} model dari '{self.config_path.name}'.")
        except json.JSONDecodeError as err:
            raise ValueError(f"[ERROR] Format JSON tidak valid pada {self.config_path}: {err}")

    def matikan_model_aktif(self) -> None:
        """Menghentikan proses llama-server yang sedang berjalan dan membersihkan VRAM."""
        if not self.current_process:
            return

        print(f"[INFO] Menghentikan model aktif: [{self.current_model}]...")
        self.current_process.terminate()

        try:
            self.current_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("[WARNING] Server tidak merespons penghentian halus. Mematikan secara paksa...")
            self.current_process.kill()

        self.current_process = None
        self.current_model = None
        time.sleep(2)  # Memberikan jeda untuk alokasi VRAM oleh driver GPU

    def pastikan_server_ready(self, timeout: Optional[int] = None) -> bool:
        """Melakukan polling ke health endpoint hingga server mengembalikan status HTTP 200."""
        batas_waktu = timeout or self.ready_timeout
        print(f"[INFO] Menunggu inisialisasi model di {self.health_url}...")
        waktu_mulai = time.time()

        while time.time() - waktu_mulai < batas_waktu:
            try:
                response = requests.get(self.health_url, timeout=1)
                if response.status_code == 200:
                    print(f"[INFO] Server siap menerima permintaan di {self.base_url}.")
                    return True
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(0.5)

        print(f"[ERROR] Batas waktu terlampaui ({batas_waktu} detik). Server gagal diinisialisasi.")
        return False

    def ganti_model(self, nama_model: str) -> bool:
        """Menjalankan proses pergantian model secara dinamis."""
        if nama_model not in self.model_configs:
            print(f"[ERROR] Model '{nama_model}' tidak terdaftar dalam konfigurasi.")
            return False

        if self.current_model == nama_model:
            print(f"[INFO] Model [{nama_model}] sudah berjalan. Perintah dilewati.")
            return True

        self.matikan_model_aktif()

        print(f"[INFO] Menjalankan model: [{nama_model}]")
        cmd = self.model_configs[nama_model]

        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.current_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        self.current_model = nama_model

        return self.pastikan_server_ready()


# if __name__ == "__main__":
#     manager = LocalModelManager(
#         config_path="model_configs.json",
#         base_url="http://127.0.0.1:8080",
#         ready_timeout=45,
#     )

#     if manager.ganti_model("qwen-4b"):
#         print(f"[INFO] Mengirim request inferensi ke: {manager.completion_url}")
#         payload: Dict[str, Any] = {
#             "messages": [{"role": "user", "content": "Halo, sebutkan nama dan versimu."}],
#             "temperature": 0.7,
#         }

#         try:
#             res = requests.post(manager.completion_url, json=payload, timeout=60)
#             if res.status_code == 200:
#                 konten = res.json()["choices"][0]["message"]["content"]
#                 print(f"[RESULT] {konten}")
#             else:
#                 print(f"[ERROR] Request gagal dengan status code: {res.status_code}")
#         except requests.RequestException as e:
#             print(f"[ERROR] Gagal melakukan request inferensi: {e}")

#     manager.matikan_model_aktif()