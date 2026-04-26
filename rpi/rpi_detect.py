#!/usr/bin/env python3
"""
RASPBERRY PI - CLIP Ad Detection + API Upload

Tento skript:
1. Sleduje zložku s frameami z TV
2. Pre každý nový frame použije CLIP na detekciu reklamy
3. Pošle výsledok + obrázok na TV Bridge API
4. Vymaže frame
5. Zapíše výsledok do CSV logu

Použitie:
  python3 rpi_detect.py <capture_dir>

  Príklad: python3 rpi_detect.py nova

Konfigurácia cez .env súbor alebo environment variables.
"""

# Load .env file if present
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, use system env vars

import sys
import time
import json
import base64
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from typing import Tuple, Optional, Dict, Any, Set
import csv
import urllib.request
import urllib.error
import io

import pickle

import numpy as np
import torch
import clip
from PIL import Image

# ============================================================
# KONFIGURÁCIA - cez .env súbor alebo environment variables
# ============================================================

# TV Bridge API
API_BASE_URL = os.environ.get("API_BASE_URL", "https://tv-bridge-api-ih76.onrender.com")
API_KEY = os.environ.get("API_KEY", "")
DEVICE_ID = os.environ.get("DEVICE_ID", "tv-1")
CHANNEL = os.environ.get("CHANNEL", "")  # e.g. "CT:1", "STV:1"
API_TIMEOUT = float(os.environ.get("API_TIMEOUT", "5.0"))

# CLIP model
CLIP_MODEL = os.environ.get("CLIP_MODEL", "ViT-B/32")
# Separate thresholds: classifier is well-calibrated (0.55), zero-shot prompts are
# heavily biased toward "ad" so require much higher confidence to avoid false positives.
THRESHOLD = float(os.environ.get("THRESHOLD", "0.55"))
THRESHOLD_ZEROSHOT = float(os.environ.get("THRESHOLD_ZEROSHOT", "0.85"))

# Temporal smoothing — window of last N frames (0 = disabled)
SMOOTH_WINDOW = int(os.environ.get("SMOOTH_WINDOW", "5"))

# Multi-frame feature averaging — average CLIP embeddings over last N frames before
# classifying. Reduces single-frame noise before it reaches the classifier (0 = disabled).
FEATURE_WINDOW = int(os.environ.get("FEATURE_WINDOW", "3"))

# Detekcia každých X sekúnd (nie každý frame)
DETECTION_INTERVAL = float(os.environ.get("DETECTION_INTERVAL", "3.0"))

# Maximálna veľkosť obrázka pre upload (šírka v px)
MAX_IMAGE_WIDTH = int(os.environ.get("MAX_IMAGE_WIDTH", "640"))

# Vylepšené Ad promptsy - špecifickejšie pre slovenské TV
AD_PROMPTS = [
    # Všeobecné reklamy
    "a television commercial advertisement",
    "a TV ad for a product or service",
    "a commercial break on television",
    "an advertisement with product branding",
    "a promotional TV spot",
    # Vizuálne znaky reklám
    "advertisement with company logo and slogan",
    "commercial with price tag or discount offer",
    "TV ad showing a product close-up",
    "advertisement with phone number or website",
    "promotional content with special offer text",
    # Špecifické typy reklám
    "car commercial on TV",
    "food or beverage advertisement",
    "pharmaceutical drug commercial",
    "bank or insurance advertisement",
    "retail store commercial",
]

NON_AD_PROMPTS = [
    # Normálny program
    "a scene from a TV show or movie",
    "television news broadcast",
    "a talk show on television",
    "sports broadcast on TV",
    "documentary footage",
    "TV series episode scene",
    "live television broadcast",
    # Špecifické programy
    "news anchor presenting news",
    "weather forecast on TV",
    "political debate or interview",
    "cooking show on television",
    "reality TV show scene",
]

# Logging
LOG_FILE = "detection_results.csv"

# Polling interval (kolko sekund cekat medzi kontrolami zlozky)
POLL_INTERVAL = 0.3

# ============================================================
# KONIEC KONFIGURÁCIE
# ============================================================


def _image_stats(img: Image.Image) -> "np.ndarray":
    img_s = img.resize((112, 112))
    arr = np.array(img_s).astype(np.float32) / 255.0
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    max_c = arr.max(axis=2)
    min_c = arr.min(axis=2)
    sat = np.where(max_c > 1e-6, (max_c - min_c) / max_c, 0.0)
    bright = max_c
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    return np.array([
        r.mean(), g.mean(), b.mean(),
        r.std(),  g.std(),  b.std(),
        sat.mean(), sat.std(),
        bright.mean(), bright.std(),
        gray.std(),
        float(np.percentile(sat, 75)),
        float(np.percentile(bright, 25)),
        float(np.percentile(bright, 75)),
    ], dtype=np.float32)


class AdDetector:
    """CLIP ad detector with trained classifier + temporal smoothing."""

    def __init__(self, device: str = None):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        print(f"[CLIP] Načítavam model {CLIP_MODEL} na {device}...")
        self.device = device
        self.model, self.preprocess = clip.load(CLIP_MODEL, device=device)
        self.model.eval()

        with torch.no_grad():
            all_prompts = AD_PROMPTS + NON_AD_PROMPTS
            text_tokens = clip.tokenize(all_prompts).to(device)
            self.text_features = self.model.encode_text(text_tokens)
            self.text_features = self.text_features / self.text_features.norm(dim=-1, keepdim=True)

        self.n_ad = len(AD_PROMPTS)
        self.n_non_ad = len(NON_AD_PROMPTS)

        self._prob_window: deque = deque(maxlen=SMOOTH_WINDOW) if SMOOTH_WINDOW > 0 else None
        self._feat_window: deque = deque(maxlen=FEATURE_WINDOW) if FEATURE_WINDOW > 0 else None

        self.classifier = None
        self.use_image_stats = False
        clf_path = Path(__file__).parent / "models" / "classifier.pkl"
        if clf_path.exists():
            with open(clf_path, "rb") as fh:
                payload = pickle.load(fh)
            self.classifier = payload["classifier"]
            self.use_image_stats = payload.get("use_image_stats", False)
            model_name = payload.get("best_model_name", "classifier")
            print(f"[CLIP] {model_name} | image_stats={self.use_image_stats} | smooth={SMOOTH_WINDOW} | feat_window={FEATURE_WINDOW}")
        else:
            print(f"[CLIP] Model načítaný. Prompts: {self.n_ad} ad, {self.n_non_ad} non-ad")

    @torch.no_grad()
    def detect(self, image_path: Path) -> Tuple[bool, float, float, Dict[str, float]]:
        """Vráti (is_ad, p_ad, p_program, details)"""
        img = Image.open(image_path).convert("RGB")
        image_input = self.preprocess(img).unsqueeze(0).to(self.device)

        image_features = self.model.encode_image(image_input)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)

        if self.classifier is not None:
            clip_feat = image_features.cpu().numpy()[0]
            if self._feat_window is not None:
                self._feat_window.append(clip_feat)
                clip_feat = np.mean(self._feat_window, axis=0)
                clip_feat = clip_feat / (np.linalg.norm(clip_feat) + 1e-8)
            feat = np.concatenate([clip_feat, _image_stats(img)]) if self.use_image_stats else clip_feat
            p_ad_raw = float(self.classifier.predict_proba(feat.reshape(1, -1))[0][1])
            mode = "classifier"
        else:
            logits = 100.0 * image_features @ self.text_features.T
            probs_all = logits.softmax(dim=-1).cpu().numpy()[0]
            p_ad_raw = float(probs_all[:self.n_ad].sum())
            mode = "prompts"

        # Temporal smoothing — recency-weighted average
        if self._prob_window is not None:
            self._prob_window.append(p_ad_raw)
            n = len(self._prob_window)
            weights = list(range(1, n + 1))
            p_ad = float(sum(w * p for w, p in zip(weights, self._prob_window)) / sum(weights))
        else:
            p_ad = p_ad_raw

        p_program = 1.0 - p_ad
        threshold = THRESHOLD if mode == "classifier" else THRESHOLD_ZEROSHOT
        is_ad = p_ad >= threshold

        details = {
            "mode": mode,
            "p_ad_raw": p_ad_raw,
            "p_ad_smoothed": p_ad,
            "threshold": threshold,
            "smooth_n": len(self._prob_window) if self._prob_window is not None else 0,
            "feat_n": len(self._feat_window) if self._feat_window is not None else 0,
        }

        return is_ad, p_ad, p_program, details


def resize_and_encode_image(image_path: Path, max_width: int = 640) -> str:
    """Zmenší obrázok a vráti base64 encoded JPEG"""
    img = Image.open(image_path).convert("RGB")

    # Resize ak je príliš veľký
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    # Encode to JPEG
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=75)
    buffer.seek(0)

    return base64.b64encode(buffer.read()).decode("ascii")


class BridgeAPI:
    """Simple API client for TV Bridge"""

    def __init__(self, base_url: str, api_key: str, device_id: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.device_id = device_id
        self.timeout = timeout

    def send_ad_result(
        self,
        is_ad: bool,
        confidence: Optional[float] = None,
        captured_at: Optional[datetime] = None,
        payload: Optional[Dict[str, Any]] = None,
        image_base64: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Odošle výsledok detekcie na API vrátane obrázka"""
        url = f"{self.base_url}/v1/ad-results"

        body = {
            "is_ad": bool(is_ad),
            "confidence": float(confidence) if confidence is not None else None,
            "captured_at": (captured_at.astimezone(timezone.utc).isoformat() if captured_at else None),
            "channel": CHANNEL or None,
            "payload": payload or {},
        }

        # Pridaj obrázok ak je k dispozícii
        if image_base64:
            body["image_base64"] = image_base64

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-API-Key": self.api_key,
                "X-Device-Id": self.device_id,
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                return (200 <= resp.status < 300, resp_body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            return False, f"HTTPError {e.code}: {detail}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def send_to_image_log(
        self,
        image_base64: str,
        is_ad: bool,
        confidence: Optional[float] = None,
        filename: Optional[str] = None,
        captured_at: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Odošle obrázok do image logu pre históriu detekcií"""
        url = f"{self.base_url}/v1/rpi/image-log"

        body = {
            "image_base64": image_base64,
            "is_ad": bool(is_ad),
            "confidence": float(confidence) if confidence is not None else None,
            "filename": filename,
            "captured_at": (captured_at.astimezone(timezone.utc).isoformat() if captured_at else None),
        }

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-API-Key": self.api_key,
                "X-Device-Id": self.device_id,
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                return (200 <= resp.status < 300, resp_body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            return False, f"HTTPError {e.code}: {detail}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


class ResultLogger:
    """CSV logger pre výsledky"""

    def __init__(self, log_file: str):
        self.log_file = Path(log_file)
        self._init_csv()

    def _init_csv(self):
        """Vytvor CSV ak neexistuje, pridaj header"""
        if not self.log_file.exists():
            with self.log_file.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "filename",
                    "is_ad",
                    "p_ad",
                    "p_program",
                    "api_success"
                ])

    def log(self, filename: str, is_ad: bool, p_ad: float, p_program: float, api_success: bool):
        """Pridaj záznam do CSV"""
        with self.log_file.open("a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                filename,
                int(is_ad),
                f"{p_ad:.4f}",
                f"{p_program:.4f}",
                int(api_success)
            ])


def get_latest_image(directory: Path) -> Optional[Path]:
    """Vráti najnovší image súbor v zložke"""
    exts = {".jpg", ".jpeg", ".png"}
    images = []

    if not directory.exists():
        return None

    for p in directory.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            images.append(p)

    if not images:
        return None

    # Vráť najnovší podľa času modifikácie
    return max(images, key=lambda p: p.stat().st_mtime)


def cleanup_old_images(directory: Path, keep_latest: bool = True):
    """Vymaže všetky obrázky okrem najnovšieho"""
    exts = {".jpg", ".jpeg", ".png"}
    images = []

    for p in directory.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            images.append(p)

    if len(images) <= 1:
        return

    # Zoraď podľa času, najnovší posledný
    images.sort(key=lambda p: p.stat().st_mtime)

    # Vymaž všetky okrem posledného
    for img in images[:-1] if keep_latest else images:
        try:
            img.unlink()
        except:
            pass


def main():
    if len(sys.argv) < 2:
        print("Použitie: python3 rpi_detect.py <capture_dir>")
        print("  Príklad: python3 rpi_detect.py nova")
        sys.exit(1)

    capture_dir = Path(sys.argv[1])

    if not capture_dir.exists():
        print(f"[ERROR] Priečinok neexistuje: {capture_dir}")
        sys.exit(1)

    print("========================================")
    print("  CLIP AD DETECTION - Raspberry Pi")
    print("========================================")
    print(f"Sledovaný priečinok: {capture_dir.absolute()}")
    print(f"API: {API_BASE_URL}")
    print(f"Device ID: {DEVICE_ID}")
    print(f"Threshold: {THRESHOLD}")
    print(f"Detection interval: {DETECTION_INTERVAL}s")
    print(f"Log: {LOG_FILE}")
    print("========================================")
    print()

    # Inicializácia
    detector = AdDetector()
    api = BridgeAPI(API_BASE_URL, API_KEY, DEVICE_ID, API_TIMEOUT)
    logger = ResultLogger(LOG_FILE)

    frame_count = 0
    last_detection_time = 0
    last_processed_file = None

    print("[INFO] Začínam sledovať zložku...")
    print("[INFO] Pre zastavenie: Ctrl+C")
    print()

    try:
        while True:
            current_time = time.time()

            # Kontroluj či uplynul interval
            if current_time - last_detection_time < DETECTION_INTERVAL:
                time.sleep(POLL_INTERVAL)
                continue

            # Nájdi najnovší obrázok
            latest_img = get_latest_image(capture_dir)

            if latest_img is None:
                time.sleep(POLL_INTERVAL)
                continue

            # Preskočíme ak je to ten istý súbor
            if last_processed_file == str(latest_img):
                time.sleep(POLL_INTERVAL)
                continue

            frame_count += 1
            last_detection_time = current_time
            last_processed_file = str(latest_img)

            try:
                # Detekcia
                t0 = time.time()
                is_ad, p_ad, p_program, details = detector.detect(latest_img)
                detect_time = time.time() - t0

                # Encode obrázok pre API
                t1 = time.time()
                image_b64 = resize_and_encode_image(latest_img, MAX_IMAGE_WIDTH)
                encode_time = time.time() - t1

                # Odošli na API (live image + ad result)
                now = datetime.now(timezone.utc)
                api_success, api_msg = api.send_ad_result(
                    is_ad=is_ad,
                    confidence=p_ad,
                    captured_at=now,
                    payload={
                        "filename": latest_img.name,
                        "p_program": p_program,
                        "detect_time": detect_time,
                        "details": details,
                    },
                    image_base64=image_b64,
                )

                # Odošli aj do image logu (pre históriu)
                log_success, _ = api.send_to_image_log(
                    image_base64=image_b64,
                    is_ad=is_ad,
                    confidence=p_ad,
                    filename=latest_img.name,
                    captured_at=now,
                )

                # Log
                logger.log(latest_img.name, is_ad, p_ad, p_program, api_success)

                # Výpis
                status = "🚨 AD" if is_ad else "✅ OK"
                api_status = "✓" if api_success else "✗"
                print(
                    f"[{frame_count:04d}] {latest_img.name:30s} | "
                    f"{status} | ad={p_ad:.2f} prog={p_program:.2f} | "
                    f"API:{api_status} | {detect_time:.2f}s"
                )

                if not api_success:
                    print(f"        API Error: {api_msg[:80]}")

                # Vyčisti staré obrázky
                cleanup_old_images(capture_dir, keep_latest=True)

            except Exception as e:
                print(f"[ERROR] Chyba pri spracovaní {latest_img.name}: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print()
        print("========================================")
        print("  ZASTAVENÉ")
        print("========================================")
        print(f"Spracovaných frameov: {frame_count}")
        print(f"Výsledky uložené v: {LOG_FILE}")
        print("========================================")


if __name__ == "__main__":
    main()
