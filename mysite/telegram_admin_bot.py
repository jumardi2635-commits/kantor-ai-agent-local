#!/usr/bin/env python3
"""
Telegram Admin Bot — AI Product Posting Module (Professional Edition + Inline Buttons)
========================================================================================
Bot Telegram untuk admin dengan wizard lengkap, mode cepat, dan tombol inline responsif.
"""

import ast
import hmac
import html
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path

import requests
from flask import request, jsonify, Response

# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# SETTING HELPER
# ------------------------------------------------------------------------------
def _get_setting(key: str, default: str = '') -> str:
    """Ambil setting dari tabel Setting atau environment variable."""
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        if s and s.value:
            return s.value
    except Exception:
        pass
    return os.getenv(key.upper(), default)

def _bot_token() -> str:
    return _get_setting('telegram_bot_token', '').strip()

def _admin_chat_id() -> str:
    return _get_setting('telegram_chat_id', '').strip()

def _openrouter_api_key() -> str:
    return _get_setting('openrouter_api_key', '').strip()

def _openrouter_model() -> str:
    return _get_setting('openrouter_model', 'meta-llama/llama-3.3-70b-instruct:free').strip()

def _openrouter_image_model() -> str:
    # Model yang mendukung OUTPUT gambar (bukan sekadar baca gambar) lewat OpenRouter,
    # mis. 'google/gemini-2.5-flash-image-preview'. Ini SENGAJA dipisah dari
    # 'openrouter_model' (model teks) karena tidak semua model mendukung
    # image generation, dan admin mungkin ingin pakai model teks yang murah
    # untuk deskripsi/spesifikasi tapi model lain khusus untuk gambar.
    return _get_setting('openrouter_image_model', 'google/gemini-2.5-flash-image-preview').strip()

def _site_name() -> str:
    return _get_setting('store_name', _get_setting('site_name', 'KEDAI PHONE'))

# ------------------------------------------------------------------------------
# AI SERVICE — OpenRouter
# ------------------------------------------------------------------------------
OPENROUTER_API_URL = 'https://openrouter.ai/api/v1/chat/completions'

def _call_openrouter(
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 800,
    debug: Optional[Dict[str, Any]] = None,
    json_mode: bool = False,
) -> Optional[str]:
    """
    Panggil OpenRouter dengan retry (3x).

    debug (opsional): jika diberikan dict kosong, akan diisi debug['error']
    dengan alasan kegagalan yang bisa ditampilkan ke admin (mis. di Telegram)
    tanpa perlu membuka log server.

    json_mode: kalau True, minta provider membalas HANYA JSON lewat parameter
    `response_format`. Sebagian model/provider "reasoning" (yang suka menulis
    proses berpikirnya dulu sebelum jawaban, mis. "We need to output JSON
    only... let's think...") jadi terpaksa langsung menjawab JSON. Kalau
    provider yang dipakai tidak mendukung parameter ini (biasanya balas HTTP
    400), otomatis di-retry TANPA parameter tersebut supaya tidak membuat
    seluruh fitur mati hanya karena satu model tidak kompatibel.
    """
    api_key = _openrouter_api_key()
    if not api_key:
        if debug is not None:
            debug['error'] = (
                "API key OpenRouter belum diisi (setting 'openrouter_api_key' "
                "atau env OPENROUTER_API_KEY kosong)."
            )
        return None

    model = _openrouter_model()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': _get_setting('openrouter_site_url', 'https://kedaiphone.pythonanywhere.com'),
        'X-Title': _site_name(),
    }
    payload = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    use_json_mode = json_mode

    last_error = None
    for attempt in range(3):
        attempt_payload = dict(payload)
        if use_json_mode:
            attempt_payload['response_format'] = {'type': 'json_object'}
        try:
            resp = requests.post(OPENROUTER_API_URL, json=attempt_payload, headers=headers, timeout=25)
            resp.raise_for_status()
            data = resp.json()
            content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '') or ''
            content = content.strip()
            if content:
                return content
            last_error = f"Respons API kosong dari model '{model}'. Payload: {json.dumps(data)[:300]}"
            logger.warning(f"[AdminBot] OpenRouter attempt {attempt + 1}: {last_error}")
        except requests.exceptions.HTTPError as e:
            body = ''
            try:
                body = (e.response.text or '')[:300]
            except Exception:
                pass
            status = e.response.status_code if e.response is not None else '?'
            last_error = f"HTTP {status} dari model '{model}': {body}"
            logger.warning(f"[AdminBot] OpenRouter attempt {attempt + 1} failed: {last_error}")
            # Sebagian model/provider menolak parameter response_format (400).
            # Lepaskan json_mode dan coba lagi di attempt berikutnya, tanpa
            # membuang sisa retry yang ada.
            if use_json_mode and status == 400:
                logger.info("[AdminBot] response_format ditolak provider, retry tanpa json_mode.")
                use_json_mode = False
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[AdminBot] OpenRouter attempt {attempt + 1} failed: {last_error}")
        if attempt < 2:
            time.sleep(2 ** attempt)

    if debug is not None and last_error:
        debug['error'] = last_error
    return None

def _generate_product_name(brand: str, model_name: str, condition: str) -> Optional[str]:
    prompt = (
        f"Buatkan nama produk HP bekas yang menarik untuk dijual online.\n"
        f"Brand: {brand}\n"
        f"Model: {model_name}\n"
        f"Kondisi: {condition}\n\n"
        f"Output HANYA nama produk (1 baris, tanpa penjelasan, tanpa tanda kutip):"
    )
    result = _call_openrouter(
        [{'role': 'system', 'content': 'Kamu adalah copywriter produk HP bekas terbaik.'},
         {'role': 'user', 'content': prompt}],
        temperature=0.8, max_tokens=60
    )
    if result:
        result = result.strip().strip('"').strip("'")
        if len(result) > 5:
            return result
    return f"{brand} {model_name} {condition}"

def _generate_product_description(brand: str, model_name: str, condition: str, price_rp: int) -> Optional[str]:
    price_str = f"{price_rp:,}".replace(',', '.')
    prompt = (
        f"Tulis deskripsi produk HP bekas untuk toko online.\n"
        f"Brand: {brand}\n"
        f"Model: {model_name}\n"
        f"Kondisi: {condition}\n"
        f"Harga: Rp {price_str}\n\n"
        f"Buat deskripsi yang menarik, profesional, SEO-friendly, maksimal 5 paragraf. "
        f"Sertakan poin keunggulan, kondisi fisik, dan garansi. "
        f"Gunakan bahasa Indonesia yang persuasif."
    )
    return _call_openrouter(
        [{'role': 'system', 'content': 'Kamu adalah copywriter produk HP bekas profesional.'},
         {'role': 'user', 'content': prompt}],
        temperature=0.7, max_tokens=600
    )

def _extract_specs_kv_pairs(text: str) -> Dict[str, str]:
    """
    Fallback paling tangguh: tarik semua pasangan "key": "value" langsung dari
    teks mentah, tanpa peduli apakah objek JSON di sekelilingnya valid/lengkap.

    Dipakai saat json.loads() dan ast.literal_eval() sama-sama gagal -- kasus
    paling umum adalah respons AI TERPOTONG (kehabisan max_tokens sebelum
    kurung kurawal penutup muncul), sehingga tidak ada JSON yang bisa diparse
    utuh, tapi pasangan key-value individualnya sendiri sudah lengkap dan bisa
    diselamatkan.
    """
    pattern = re.compile(r'["\']([a-zA-Z_][\w \-]{1,40})["\']\s*:\s*["\']([^"\']{1,300})["\']')
    pairs: Dict[str, str] = {}
    for key, value in pattern.findall(text):
        key = key.strip().lower().replace(' ', '_')
        value = value.strip()
        if key and value and value.lower() not in ('-', '—', 'n/a', 'null', 'none'):
            pairs[key] = value
    return pairs

def _generate_product_specs(
    brand: str, model_name: str, debug: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Generate spesifikasi LENGKAP secara otomatis sesuai jenis produk (smartphone,
    tablet, smartwatch, atau aksesoris) berdasarkan data resmi model tersebut.

    debug (opsional): jika diberikan dict kosong, akan diisi:
      - debug['raw']   -> potongan respons mentah AI (untuk ditampilkan ke admin)
      - debug['error'] -> alasan kegagalan dalam bahasa manusia
      - debug['note']  -> catatan jika hasil dipulihkan lewat fallback parsial

    Urutan strategi parsing (dari paling ketat ke paling toleran):
      1. json.loads() pada blok {...} pertama yang ditemukan.
      2. ast.literal_eval() -- menangani model yang membalas gaya dict Python
         dengan tanda kutip tunggal.
      3. _extract_specs_kv_pairs() -- menarik pasangan key-value satu per satu
         walau objek JSON-nya tidak valid/tidak lengkap (paling sering karena
         respons terpotong akibat max_tokens habis).
    """
    prompt = (
        f"Kamu akan mengisi spesifikasi teknis LENGKAP dan AKURAT untuk produk gadget berikut, "
        f"berdasarkan data resmi/rilis produk aslinya (bukan karangan generik).\n\n"
        f"Brand: {brand}\n"
        f"Model: {model_name}\n\n"
        f"Tentukan dulu jenis produknya (smartphone, tablet, smartwatch, atau aksesoris/lainnya), "
        f"lalu isi field JSON yang RELEVAN dengan jenis tersebut saja:\n\n"
        f"Untuk SMARTPHONE/TABLET gunakan field:\n"
        f'{{"layar": "...", "prosesor": "...", "ram": "...", "penyimpanan": "...", '
        f'"kamera_belakang": "...", "kamera_depan": "...", "baterai": "...", "os": "...", '
        f'"jaringan": "...", "dimensi": "...", "berat": "...", "warna_tersedia": "...", '
        f'"konektivitas": "...", "ketahanan_air": "...", "kelengkapan_paket": "..."}}\n\n'
        f"Untuk SMARTWATCH gunakan field:\n"
        f'{{"layar": "...", "baterai": "...", "sensor": "...", "ketahanan_air": "...", '
        f'"konektivitas": "...", "kompatibilitas": "...", "material_tali": "...", '
        f'"fitur_kesehatan": "...", "dimensi": "...", "berat": "..."}}\n\n'
        f'Untuk AKSESORIS gunakan field yang relevan saja (mis. "bahan", "kompatibilitas", '
        f'"warna_tersedia", "dimensi", "berat", "fitur").\n\n'
        f"Aturan:\n"
        f"- WAJIB isi SEMUA field pada daftar jenis produk yang sesuai di atas — JANGAN ada field "
        f"yang dihilangkan/dilewati dari output, karena hasil ini akan ditampilkan langsung ke "
        f"pembeli di halaman produk dan harus terlihat lengkap.\n"
        f"- Prioritaskan data SPESIFIK dan akurat sesuai model aslinya (angka RAM/penyimpanan/kamera "
        f"aktual, bukan placeholder generik seperti '-' atau 'N/A').\n"
        f"- Kalau nama model persis tidak kamu kenali dengan yakin (mis. varian/rilis baru yang "
        f"kurang familiar), JANGAN mengosongkan atau menghapus field itu. Sebagai gantinya, berikan "
        f"estimasi paling masuk akal berdasarkan seri/generasi terdekat dari brand yang sama (mis. "
        f"model pendahulu atau model sekelas yang sudah rilis resmi), dan field tetap harus terisi.\n"
        f"- Buat setiap value SERINGKAS mungkin (maksimal 8-10 kata) supaya seluruh objek muat "
        f"dalam sekali jawaban tanpa terpotong.\n"
        f"- JANGAN menuliskan proses berpikir, analisis, keraguan, atau kalimat apa pun sebelum "
        f"JSON (contoh yang DILARANG: 'We need to...', 'Let's think...', 'Saya akan...'). "
        f"Karakter PERTAMA pada balasanmu harus langsung tanda '{{'.\n"
        f"- Output HANYA JSON valid satu level (key-value string) dengan tanda kutip GANDA (\"), "
        f"tanpa markdown code block, tanpa komentar, tanpa penjelasan tambahan sama sekali, dan "
        f"SEMUA field yang wajib di atas harus ada di objek JSON tersebut."
    )
    debug_call: Dict[str, Any] = {}
    result = _call_openrouter(
        [{'role': 'system', 'content': (
            'Kamu adalah ahli spesifikasi gadget yang sangat detail dan akurat, hafal data resmi '
            'rilis setiap produk. Kamu SELALU mengisi SEMUA field yang diminta secara lengkap — kalau '
            'model persisnya tidak yakin, pakai estimasi dari seri/generasi terdekat daripada '
            'mengosongkan/menghapus field. Kamu SELALU menjawab dengan JSON valid bertanda kutip '
            'ganda saja, TANPA proses berpikir/reasoning/penjelasan apa pun di luar objek JSON — '
            'balasanmu harus dimulai langsung dari karakter "{" dan diakhiri "}"; jaga jawaban tetap '
            'ringkas agar tidak terpotong.'
        )},
         {'role': 'user', 'content': prompt}],
        temperature=0.2, max_tokens=2000,
        debug=debug_call, json_mode=True,
    )

    if debug is not None:
        debug['raw'] = result or ''

    if not result:
        err = debug_call.get('error') or 'AI tidak mengembalikan hasil apa pun.'
        logger.warning(f"[AdminBot] Gagal generate specs untuk {brand} {model_name}: {err}")
        if debug is not None:
            debug['error'] = err
        return None

    # Buang markdown code fence jika ada (```json ... ```)
    cleaned = re.sub(r'```(?:json)?\s*|\s*```', '', result).strip()

    # Sebagian model "reasoning" membungkus proses berpikirnya dalam tag
    # <think>...</think> (atau <reasoning>...</reasoning>) sebelum jawaban asli.
    # Buang blok itu dulu supaya tidak ikut ke tahap ekstraksi JSON di bawah.
    cleaned = re.sub(r'<(think|reasoning)>.*?</\1>', '', cleaned, flags=re.DOTALL | re.IGNORECASE).strip()

    # Ambil hanya blok {...} pertama jika ada; kalau tidak ketemu (mis. respons
    # terpotong sebelum ada kurung tutup), lanjutkan dengan teks mentahnya --
    # fallback kv-pair di bawah tetap bisa menyelamatkan sebagian data.
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    parsed = None
    parse_error = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        parse_error = f"JSON tidak valid ({e})"
        # Fallback 1: kadang model membalas gaya dict Python (kutip tunggal)
        try:
            parsed = ast.literal_eval(cleaned)
        except (ValueError, SyntaxError):
            parsed = None

    if not isinstance(parsed, dict):
        # Fallback 2: tarik pasangan key-value langsung, tahan terhadap JSON
        # yang terpotong/tidak lengkap (kasus paling umum: kehabisan token).
        kv = _extract_specs_kv_pairs(cleaned)
        if kv:
            logger.info(
                f"[AdminBot] Specs {brand} {model_name} dipulihkan lewat fallback kv-pair "
                f"({len(kv)} field) setelah parsing utama gagal: {parse_error}"
            )
            if debug is not None:
                debug['note'] = (
                    f"JSON dari AI tidak lengkap/valid ({parse_error}), tapi {len(kv)} field "
                    f"berhasil dipulihkan sebagian."
                )
            return kv

        err = parse_error or 'Hasil AI bukan JSON/dict yang bisa dibaca.'
        logger.warning(f"[AdminBot] Gagal parse JSON specs {brand} {model_name}: {err} | raw: {result[:300]}")
        if debug is not None:
            debug['error'] = err
        return None

    # Buang field kosong / placeholder yang tidak berguna
    cleaned_specs = {
        str(k): v for k, v in parsed.items()
        if v and str(v).strip().lower() not in ('-', '—', 'n/a', 'null', 'none')
    }
    if not cleaned_specs and debug is not None:
        debug['error'] = 'AI membalas JSON valid, tapi semua field kosong/placeholder.'
    return cleaned_specs or None

def _build_image_prompt(brand: str, model_name: str, condition: str = '') -> str:
    """Susun prompt foto produk yang konsisten untuk katalog e-commerce HP bekas."""
    condition_txt = f", kondisi fisik {condition}" if condition else ""
    return (
        f"Professional e-commerce product photography of a {brand} {model_name} "
        f"smartphone{condition_txt}. Clean pure white studio background, soft even "
        f"lighting from the front, device centered and shown at a slight 3/4 angle, "
        f"sharp focus, photorealistic, high resolution, no text, no watermark, "
        f"no logo overlay, no people, no hands, no reflections of a photographer."
    )

def _call_openrouter_image(prompt: str, debug: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Panggil OpenRouter untuk GENERATE GAMBAR (bukan teks) lewat model yang
    mendukung modalitas output image (mis. google/gemini-2.5-flash-image-preview).

    Berbeda dari _call_openrouter() yang dipakai untuk nama/deskripsi/spesifikasi
    (yang selalu mengharapkan balasan berupa teks), fungsi ini:
      - Mengirim parameter 'modalities': ['image', 'text'] sesuai spesifikasi
        image generation OpenRouter.
      - Membaca gambar dari choices[0].message.images (format resmi OpenRouter),
        dengan fallback membaca data-URI base64 langsung dari 'content' kalau
        provider tertentu membalas dengan cara berbeda.
      - Mendukung dua bentuk hasil gambar: data-URI base64 (didekode langsung)
        atau URL biasa (diunduh terpisah).

    Mengembalikan {'data': bytes, 'mime_type': str} jika berhasil, None jika
    gagal — dengan debug['error'] berisi alasan kegagalan yang bisa
    ditampilkan langsung ke admin di Telegram (mis. model tidak mendukung
    image generation, API key salah, quota habis, dsb) tanpa perlu membuka
    log server.
    """
    api_key = _openrouter_api_key()
    if not api_key:
        if debug is not None:
            debug['error'] = (
                "API key OpenRouter belum diisi (setting 'openrouter_api_key' "
                "atau env OPENROUTER_API_KEY kosong)."
            )
        return None

    model = _openrouter_image_model()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': _get_setting('openrouter_site_url', 'https://kedaiphone.pythonanywhere.com'),
        'X-Title': _site_name(),
    }
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'modalities': ['image', 'text'],
    }

    last_error = None
    for attempt in range(3):
        try:
            resp = requests.post(OPENROUTER_API_URL, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            last_error = f"Timeout menunggu balasan dari model '{model}' (lebih dari 60 detik)."
            logger.warning(f"[AdminBot] OpenRouter image attempt {attempt + 1}: {last_error}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.HTTPError as e:
            body = ''
            try:
                body = (e.response.text or '')[:300]
            except Exception:
                pass
            status = e.response.status_code if e.response is not None else '?'
            last_error = f"HTTP {status} dari model '{model}': {body}"
            logger.warning(f"[AdminBot] OpenRouter image attempt {attempt + 1} failed: {last_error}")
            # 400/404 biasanya berarti model salah/tidak ada, atau model ini
            # memang tidak mendukung image generation — retry percuma.
            if status in (400, 404):
                break
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"Gagal menghubungi OpenRouter: {type(e).__name__}: {e}"
            logger.warning(f"[AdminBot] OpenRouter image attempt {attempt + 1} failed: {last_error}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[AdminBot] OpenRouter image attempt {attempt + 1} failed: {last_error}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

        choices = data.get('choices') or []
        if not choices:
            last_error = f"Respons API tidak berisi 'choices'. Payload: {json.dumps(data)[:300]}"
            logger.warning(f"[AdminBot] OpenRouter image attempt {attempt + 1}: {last_error}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

        message = choices[0].get('message', {}) or {}
        image_url = None

        # Bentuk resmi OpenRouter: message.images = [{'type': 'image_url', 'image_url': {'url': 'data:...'}}]
        for img in (message.get('images') or []):
            if isinstance(img, dict):
                candidate = (img.get('image_url') or {}).get('url')
                if candidate:
                    image_url = candidate
                    break

        # Fallback: sebagian provider menaruh data-URI base64 langsung di 'content'.
        if not image_url:
            content = message.get('content', '') or ''
            match = re.search(r'data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=]+)', content)
            if match:
                image_url = f"data:{match.group(1)};base64,{match.group(2)}"

        if not image_url:
            finish_reason = choices[0].get('finish_reason', '-')
            last_error = (
                f"Model '{model}' tidak mengembalikan gambar apa pun "
                f"(finish_reason: {finish_reason}). Kemungkinan model ini tidak "
                f"mendukung image generation, atau permintaan ditolak oleh "
                f"content filter provider."
            )
            logger.warning(f"[AdminBot] OpenRouter image attempt {attempt + 1}: {last_error}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

        if image_url.startswith('data:'):
            try:
                header, b64data = image_url.split(',', 1)
                mime_match = re.match(r'data:([\w/+.-]+);base64', header)
                mime_type = mime_match.group(1) if mime_match else 'image/png'
                import base64 as _b64
                raw = _b64.b64decode(b64data)
            except Exception as e:
                last_error = f"Gagal decode base64 gambar dari model '{model}': {type(e).__name__}: {e}"
                logger.warning(f"[AdminBot] {last_error}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                continue
            if not raw:
                last_error = "Data gambar base64 kosong setelah didekode."
                if attempt < 2:
                    time.sleep(2 ** attempt)
                continue
            return {'data': raw, 'mime_type': mime_type}
        else:
            # URL biasa (bukan data-URI) — unduh terpisah.
            try:
                img_resp = requests.get(image_url, timeout=30)
                img_resp.raise_for_status()
                content_type = img_resp.headers.get('content-type', 'image/png').split(';')[0].strip()
                if content_type.startswith('image/') and img_resp.content:
                    return {'data': img_resp.content, 'mime_type': content_type}
                last_error = f"URL gambar dari AI bukan gambar valid (content-type: {content_type})."
            except Exception as e:
                last_error = f"Gagal mengunduh gambar dari URL yang diberikan AI: {type(e).__name__}: {e}"
            logger.warning(f"[AdminBot] {last_error}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

    if debug is not None and last_error:
        debug['error'] = last_error
    return None

def _generate_product_image(
    brand: str, model_name: str, condition: str = '', debug: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Generate gambar produk otomatis berdasarkan brand + model (+ kondisi opsional)
    lewat model image-capable OpenRouter. Mengembalikan {'data': bytes, 'mime_type': str}
    atau None jika gagal (lihat debug['error'] untuk alasannya).
    """
    if not (brand or '').strip() and not (model_name or '').strip():
        if debug is not None:
            debug['error'] = "Brand dan model kosong, tidak cukup informasi untuk membuat gambar."
        return None
    prompt = _build_image_prompt(brand, model_name, condition)
    return _call_openrouter_image(prompt, debug=debug)

def _generate_and_attach_product_image(
    product_id: int, brand: str, model_name: str, condition: str = '',
    debug: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Alur lengkap untuk produk yang SUDAH ADA di database: generate gambar via AI,
    simpan ke disk, lalu tempelkan ke slot gambar kosong produk tersebut.
    Menggunakan _assign_image_to_product() yang sama dengan alur upload foto
    manual, supaya kedua alur konsisten (satu sumber kebenaran untuk "cari slot
    kosong & simpan ke DB").
    """
    result = _generate_product_image(brand, model_name, condition, debug=debug)
    if not result:
        return False

    filename = _save_image_bytes(
        result['data'], result.get('mime_type', 'image/png'), filename_prefix='ai_product'
    )
    if not filename:
        if debug is not None:
            debug['error'] = debug.get('error') or (
                'Gambar berhasil digenerate oleh AI, tapi gagal disimpan ke disk '
                '(cek permission folder static/uploads/products/).'
            )
        return False

    if not _assign_image_to_product(product_id, filename):
        if debug is not None:
            debug['error'] = debug.get('error') or (
                f'Gambar berhasil dibuat dan disimpan, tapi gagal ditempelkan ke produk '
                f'ID {product_id} (produk tidak ditemukan, atau 4 slot gambar sudah penuh).'
            )
        return False

    return True

def _suggest_price(brand: str, model_name: str, condition: str) -> Optional[int]:
    prompt = (
        f"Berikan estimasi harga jual HP bekas {brand} {model_name} kondisi {condition} "
        f"dalam pasaran Indonesia tahun 2026. Output HANYA angka dalam Rupiah "
        f"(tanpa 'Rp', tanpa titik/koma pemisah ribuan, tanpa penjelasan). Contoh output: 2500000"
    )
    result = _call_openrouter(
        [{'role': 'system', 'content': 'Kamu adalah appraiser HP bekas profesional di Indonesia.'},
         {'role': 'user', 'content': prompt}],
        temperature=0.3, max_tokens=30
    )
    if result:
        try:
            numbers = re.findall(r'[\d,]+\.?\d*', result.replace('.', '').replace(',', ''))
            if numbers:
                rupiah = float(numbers[0])
                return int(rupiah * 100)
        except (ValueError, IndexError):
            pass
    return None

# ------------------------------------------------------------------------------
# RATE LIMITING
# ------------------------------------------------------------------------------
class _RateLimiter:
    _storage = defaultdict(list)

    @classmethod
    def is_allowed(cls, key: str, limit: int = 30, window: int = 60) -> bool:
        now = time.time()
        cls._storage[key] = [t for t in cls._storage[key] if now - t < window]
        if len(cls._storage[key]) >= limit:
            return False
        cls._storage[key].append(now)
        return True

def _escape_html(text: str) -> str:
    return html.escape(text or '', quote=False)

# ------------------------------------------------------------------------------
# TELEGRAM API HELPERS
# ------------------------------------------------------------------------------
def _send_message(chat_id: int, text: str, parse_mode: str = 'HTML', reply_markup: dict = None) -> Optional[int]:
    """Kirim pesan. Mengembalikan message_id jika berhasil, None jika gagal."""
    token = _bot_token()
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode, 'disable_web_page_preview': True}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result.get('ok'):
            return result.get('result', {}).get('message_id')
        return None
    except Exception as e:
        logger.error(f"[AdminBot] Gagal kirim pesan: {e}")
        return None

def _send_photo(chat_id: int, photo_url: str, caption: str = None, reply_markup: dict = None) -> bool:
    token = _bot_token()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {'chat_id': chat_id, 'photo': photo_url}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = 'HTML'
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=data, timeout=15)
        return resp.json().get('ok', False)
    except Exception as e:
        logger.error(f"[AdminBot] Gagal kirim foto: {e}")
        return False

def _answer_callback(callback_query_id: str, text: str = None, show_alert: bool = False) -> bool:
    token = _bot_token()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    data = {'callback_query_id': callback_query_id}
    if text:
        data['text'] = text[:200]
        data['show_alert'] = show_alert
    try:
        resp = requests.post(url, json=data, timeout=10)
        return resp.json().get('ok', False)
    except Exception as e:
        logger.error(f"[AdminBot] Gagal answer callback: {e}")
        return False

def _edit_message_text(chat_id: int, message_id: int, text: str, parse_mode: str = 'HTML', reply_markup: dict = None) -> bool:
    token = _bot_token()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    data = {
        'chat_id': chat_id,
        'message_id': message_id,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': True,
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if result.get('ok'):
            return True
        # Jika pesan tidak berubah (Telegram error "message is not modified"), anggap sukses
        if 'message is not modified' in json.dumps(result):
            return True
        return False
    except Exception as e:
        logger.error(f"[AdminBot] Gagal edit pesan: {e}")
        return False

def _edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: dict = None) -> bool:
    token = _bot_token()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    data = {'chat_id': chat_id, 'message_id': message_id}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=data, timeout=10)
        return resp.json().get('ok', False)
    except Exception as e:
        logger.error(f"[AdminBot] Gagal edit reply markup: {e}")
        return False

# ------------------------------------------------------------------------------
# INLINE KEYBOARD BUILDERS
# ------------------------------------------------------------------------------
def _kb(rows: List[List[Dict[str, str]]]) -> Dict[str, Any]:
    """Helper singkat untuk membangun struktur inline_keyboard."""
    return {'inline_keyboard': rows}

def _btn(text: str, callback_data: str) -> Dict[str, str]:
    return {'text': text, 'callback_data': callback_data}

def _main_menu_keyboard() -> Dict[str, Any]:
    return _kb([
        [_btn('📝 Posting Produk (Wizard)', 'menu:postproduk'), _btn('⚡ Mode Cepat', 'menu:fastpost')],
        [_btn('📦 Daftar Produk', 'menu:listproduk'), _btn('🤖 AI Generate', 'menu:ai')],
        [_btn('📷 Tambah Foto', 'menu:addphoto'), _btn('🖼️ Generate Foto AI', 'menu:genfoto')],
        [_btn('❓ Bantuan', 'menu:bantuan')],
    ])

def _specifications_keyboard(has_draft: bool) -> Dict[str, Any]:
    rows = []
    if has_draft:
        rows.append([_btn('✅ Gunakan Spesifikasi Ini', 'wiz:specs_accept')])
        rows.append([_btn('🔄 Generate Ulang', 'wiz:specs_regenerate')])
    else:
        rows.append([_btn('🔄 Coba Generate Lagi', 'wiz:specs_regenerate')])
    rows.append([_btn('⏭ Lewati', 'wiz:specs_skip')])
    rows.append([_btn('⬅️ Kembali', 'wiz:back')])
    return _kb(rows)

def _condition_keyboard() -> Dict[str, Any]:
    return _kb([
        [_btn('✨ Like New', 'wiz:condition:Like New'), _btn('👍 Excellent', 'wiz:condition:Excellent')],
        [_btn('🙂 Good', 'wiz:condition:Good'), _btn('😐 Fair', 'wiz:condition:Fair')],
        [_btn('⬅️ Kembali', 'wiz:back')],
    ])

def _category_keyboard() -> Dict[str, Any]:
    return _kb([
        [_btn('📱 Smartphone', 'wiz:category:Smartphone'), _btn('📲 Tablet', 'wiz:category:Tablet')],
        [_btn('⌚ Smartwatch', 'wiz:category:Smartwatch'), _btn('🎧 Aksesoris', 'wiz:category:Aksesoris')],
        [_btn('⬅️ Kembali', 'wiz:back')],
    ])

def _compare_price_keyboard() -> Dict[str, Any]:
    return _kb([
        [_btn('⏭ Tidak Ada (Skip)', 'wiz:compare_skip')],
        [_btn('⬅️ Kembali', 'wiz:back')],
    ])

def _description_keyboard() -> Dict[str, Any]:
    return _kb([
        [_btn('🤖 Pakai AI Generate', 'wiz:desc_skip')],
        [_btn('⬅️ Kembali', 'wiz:back')],
    ])

def _images_keyboard(count: int) -> Dict[str, Any]:
    rows = []
    if count < 4:
        rows.append([_btn('🤖 Generate Foto AI', 'wiz:images_ai')])
    if count > 0:
        rows.append([_btn(f'✅ Selesai ({count}/4)', 'wiz:images_done')])
    rows.append([_btn('⏭ Tanpa Foto (Skip)', 'wiz:images_skip')])
    rows.append([_btn('⬅️ Kembali', 'wiz:back')])
    return _kb(rows)

def _confirm_keyboard() -> Dict[str, Any]:
    return _kb([
        [_btn('✅ Ya, Posting', 'wiz:confirm_yes')],
        [_btn('🔄 Ulang', 'wiz:confirm_restart'), _btn('❌ Batal', 'wiz:confirm_no')],
    ])

def _stock_keyboard() -> Dict[str, Any]:
    return _kb([
        [_btn('1', 'wiz:stock:1'), _btn('2', 'wiz:stock:2'), _btn('5', 'wiz:stock:5')],
        [_btn('⬅️ Kembali', 'wiz:back')],
    ])

def _delete_confirm_keyboard(product_id: int) -> Dict[str, Any]:
    return _kb([
        [_btn('✅ Ya, hapus', f'delete_confirm_{product_id}'),
         _btn('❌ Batal', f'delete_cancel_{product_id}')],
    ])

def _product_row_keyboard(product_id: int) -> Dict[str, Any]:
    return _kb([
        [_btn('✏️ Edit', f'menu:edit_{product_id}'),
         _btn('📷 +Foto', f'menu:addphoto_{product_id}'),
         _btn('🗑️ Hapus', f'delete_ask_{product_id}')],
        [_btn('🤖 Generate Foto AI', f'menu:genfoto_{product_id}')],
    ])

def _back_to_menu_keyboard() -> Dict[str, Any]:
    return _kb([[_btn('⬅️ Menu Utama', 'menu:bantuan')]])

# ------------------------------------------------------------------------------
# FILE DOWNLOAD & STORAGE
# ------------------------------------------------------------------------------
def _products_upload_dir() -> Path:
    """
    Folder tujuan penyimpanan gambar produk — HARUS sama persis dengan
    UPLOAD_PRODUCTS di app.py (os.path.join(Config.UPLOAD_FOLDER, 'products')
    == 'static/uploads/products'), dan dilayani oleh route uploaded_product_file().
    Path dibangun relatif terhadap lokasi file ini (bukan CWD proses), supaya
    tetap benar walau app.py di-run dari direktori kerja yang berbeda.
    Dipakai bersama oleh download foto Telegram maupun penyimpanan gambar
    hasil generate AI, supaya keduanya konsisten satu folder.
    """
    base_dir = Path(__file__).resolve().parent
    upload_dir = base_dir / 'static' / 'uploads' / 'products'
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir

# Mapping mime-type -> ekstensi file, dipakai saat menyimpan gambar hasil AI
# generate (yang datang sebagai bytes + mime_type, bukan file upload biasa).
_MIME_EXT_MAP = {
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'image/gif': '.gif',
}

def _save_image_bytes(image_bytes: bytes, mime_type: str = 'image/png', filename_prefix: str = 'product') -> Optional[str]:
    """
    Simpan bytes gambar mentah (mis. hasil decode base64 dari AI generate) ke
    folder static/uploads/products/, dengan penanganan error lengkap. Berbeda
    dari _download_telegram_file() yang mengunduh dari URL Telegram, fungsi
    ini menerima bytes yang sudah ada di memori.

    Mengembalikan NAMA FILE POLOS (tanpa prefix folder), sama seperti
    _download_telegram_file(), supaya kompatibel dengan skema penyimpanan
    Product.image_filename yang sudah ada.
    """
    if not image_bytes:
        logger.error("[AdminBot] _save_image_bytes dipanggil dengan bytes kosong")
        return None

    mime_key = (mime_type or '').lower().split(';')[0].strip()
    ext = _MIME_EXT_MAP.get(mime_key, '.png')
    filename = f"{filename_prefix}_{uuid.uuid4().hex[:8]}{ext}"

    try:
        upload_dir = _products_upload_dir()
    except Exception as e:
        logger.error(f"[AdminBot] Gagal menyiapkan folder upload untuk gambar AI: {e}")
        return None

    full_path = upload_dir / filename
    try:
        with open(full_path, 'wb') as f:
            f.write(image_bytes)

        if not full_path.exists() or full_path.stat().st_size == 0:
            logger.error(f"[AdminBot] File gambar AI tersimpan kosong atau tidak ada: {full_path}")
            full_path.unlink(missing_ok=True)
            return None

        logger.info(f"[AdminBot] Gambar AI tersimpan: {full_path} (size: {full_path.stat().st_size} bytes)")
        return filename
    except OSError as e:
        logger.error(f"[AdminBot] Gagal menulis file gambar AI ke disk ({full_path}): {e}")
        try:
            full_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    except Exception as e:
        logger.error(f"[AdminBot] Gagal menyimpan gambar AI (error tak terduga): {e}")
        try:
            full_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None

def _download_telegram_file(file_id: str) -> Optional[str]:
    """
    Unduh file dari Telegram dan simpan ke folder static/uploads/products/ —
    folder yang SAMA persis dengan yang dipakai save_uploaded_image() di
    app.py (UPLOAD_PRODUCTS) dan yang dilayani oleh route uploaded_product_file().
    Mengembalikan NAMA FILE POLOS saja (tanpa prefix folder apa pun), karena
    itulah format yang disimpan admin/product_form.html ke kolom
    Product.image_filename — kalau ikut menyimpan prefix 'uploads/' di sini,
    url_for('uploaded_product_file', filename=...) akan membentuk path ganda
    (.../static/uploads/products/uploads/...) dan gambar jadi 404/rusak saat
    dibuka di admin maupun katalog.
    """
    token = _bot_token()
    if not token:
        logger.error("[AdminBot] Token bot tidak ditemukan untuk download file")
        return None

    get_url = f"https://api.telegram.org/bot{token}/getFile"
    try:
        resp = requests.get(get_url, params={'file_id': file_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('ok'):
            logger.error(f"[AdminBot] getFile gagal: {data}")
            return None
        file_path = data['result']['file_path']
        logger.info(f"[AdminBot] File path dari Telegram: {file_path}")
    except Exception as e:
        logger.error(f"[AdminBot] Gagal mendapatkan file path: {e}")
        return None

    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        resp = requests.get(download_url, stream=True, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            logger.warning(f"[AdminBot] File bukan gambar: {content_type}")
    except Exception as e:
        logger.error(f"[AdminBot] Gagal mengunduh file: {e}")
        return None

    ext = os.path.splitext(file_path)[1] or '.jpg'
    filename = f"product_{uuid.uuid4().hex[:8]}{ext}"

    try:
        upload_dir = _products_upload_dir()
    except Exception as e:
        logger.error(f"[AdminBot] Gagal menyiapkan folder upload: {e}")
        return None
    full_path = upload_dir / filename

    try:
        with open(full_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        if not full_path.exists() or full_path.stat().st_size == 0:
            logger.error(f"[AdminBot] File tersimpan kosong atau tidak ada: {full_path}")
            full_path.unlink(missing_ok=True)
            return None

        logger.info(f"[AdminBot] Foto tersimpan: {full_path} (size: {full_path.stat().st_size} bytes)")
        # Nama file POLOS saja — samakan dengan konvensi save_uploaded_image() di app.py
        return filename
    except Exception as e:
        logger.error(f"[AdminBot] Gagal menyimpan file: {e}")
        full_path.unlink(missing_ok=True)
        return None

# ------------------------------------------------------------------------------
# PRODUCT CRUD HELPERS
# ------------------------------------------------------------------------------
def _generate_slug(name: str) -> str:
    base = re.sub(r'[^\w\s-]', '', name.lower().strip())
    base = re.sub(r'[\s]+', '-', base)[:150]
    unique = str(uuid.uuid4())[:6]
    return f"{base}-{unique}"

def _format_price(price_in_cents: Optional[int]) -> str:
    """Format harga (disimpan dalam satuan sen/×100) menjadi format Rupiah, contoh: Rp 2.500.000"""
    if price_in_cents is None:
        return '—'
    try:
        rupiah = float(price_in_cents) / 100.0
    except (TypeError, ValueError):
        rupiah = 0.0
    formatted = f"{rupiah:,.0f}".replace(',', '.')
    return f"Rp {formatted}"

def _parse_int(value: str) -> Optional[int]:
    if not value or value.strip() == '':
        return None
    clean = re.sub(r'[^\d.]', '', value.replace(',', ''))
    try:
        return int(float(clean))
    except ValueError:
        return None

def _generate_fallback_description(brand: str, model: str, condition: str, price_rp: int) -> str:
    price_str = f"{price_rp:,.0f}".replace(',', '.')
    return (
        f"📱 {brand} {model} dalam kondisi {condition} siap untuk Anda.\n\n"
        f"HP ini telah melalui pengecekan kualitas dan dipastikan berfungsi dengan baik.\n"
        f"Dengan harga Rp {price_str}, ini adalah tawaran yang sangat menarik.\n\n"
        f"Segera miliki {brand} {model} ini sebelum kehabisan!"
    )

def _create_product_from_data(data: Dict[str, Any]) -> Optional[Any]:
    """Buat produk dari dictionary data (digunakan oleh wizard dan fastpost)."""
    try:
        from models import Product, db

        name = data.get('name') or f"{data.get('brand', '')} {data.get('model', '')} {data.get('condition', '')}".strip()
        slug = _generate_slug(name)
        existing = Product.query.filter_by(slug=slug).first()
        if existing:
            slug = f"{slug}-{str(uuid.uuid4())[:4]}"

        price = data.get('price', 0)
        if isinstance(price, str):
            price = int(price)

        compare_at_price = data.get('compare_at_price')
        if compare_at_price is not None and isinstance(compare_at_price, str):
            compare_at_price = int(compare_at_price) if compare_at_price.strip() else None

        # PENTING: internal wizard/AI menyimpan specs sebagai dict biasa
        # {"ram": "8GB", ...}, tapi admin/product_form.html (Alpine.js)
        # mengharapkan format list [{"key": "...", "value": "..."}, ...]
        # (persis seperti yang dikirim form admin lewat hidden input
        # `specifications`). Kalau disimpan sebagai dict mentah, halaman
        # edit produk di admin akan error (x-for/`.filter()` Alpine gagal
        # karena formData.specifications bukan array) begitu admin
        # membuka produk yang diposting lewat bot ini.
        specs_raw = data.get('specifications')
        if specs_raw and isinstance(specs_raw, dict):
            specs = [
                {'key': str(k).replace('_', ' ').strip().title(), 'value': str(v).strip()}
                for k, v in specs_raw.items()
                if v and str(v).strip()
            ]
            specs = specs or None
        elif specs_raw and isinstance(specs_raw, list):
            # Sudah dalam format yang benar (mis. diteruskan dari sumber lain) — saring saja.
            specs = [
                s for s in specs_raw
                if isinstance(s, dict) and s.get('key') and str(s.get('value', '')).strip()
            ]
            specs = specs or None
        else:
            specs = None

        image_filenames = data.get('image_filenames', [])
        image_filenames = [f for f in image_filenames if f and f.strip()]

        image_filename = image_filenames[0] if image_filenames else ''
        image_filename2 = image_filenames[1] if len(image_filenames) > 1 else ''
        image_filename3 = image_filenames[2] if len(image_filenames) > 2 else ''
        image_filename4 = image_filenames[3] if len(image_filenames) > 3 else ''

        description = data.get('description', '')
        if not description:
            description = _generate_fallback_description(
                data.get('brand', ''),
                data.get('model', ''),
                data.get('condition', ''),
                price // 100
            )

        product = Product(
            name=name,
            slug=slug,
            brand=data.get('brand', ''),
            model=data.get('model', ''),
            sku=data.get('sku', f"SKU-{uuid.uuid4().hex[:6].upper()}"),
            category=data.get('category', 'Smartphone'),
            price=price,
            compare_at_price=compare_at_price,
            condition=data.get('condition', 'Like New'),
            stock=data.get('stock', 1),
            low_stock_alert=2,
            short_description=data.get('short_description', ''),
            description=description,
            specifications=specs,
            status='published',
            is_active=True,
            image_filename=image_filename,
            image_filename2=image_filename2,
            image_filename3=image_filename3,
            image_filename4=image_filename4,
        )
        db.session.add(product)
        db.session.commit()
        logger.info(f"[AdminBot] Produk berhasil dibuat: {product.name} (ID: {product.id}) dengan {len(image_filenames)} gambar")
        return product
    except Exception as e:
        logger.error(f"[AdminBot] Gagal create product: {e}", exc_info=True)
        try:
            from models import db
            db.session.rollback()
        except Exception:
            pass
        return None

def _assign_image_to_product(product_id: int, filename: str) -> bool:
    """
    Tempelkan NAMA FILE gambar yang SUDAH tersimpan di disk (di folder
    static/uploads/products/) ke slot kosong pertama milik produk
    (image_filename..image_filename4) dan commit ke DB.

    Dipisah dari proses "unduh dari Telegram" supaya bisa dipakai ulang oleh
    dua alur berbeda yang sama-sama berakhir dengan "ada file gambar di disk,
    tinggal ditempelkan ke produk": (1) foto asli yang dikirim admin lewat
    Telegram — lihat _add_photo_to_product(); dan (2) gambar yang di-generate
    otomatis oleh AI — lihat _generate_and_attach_product_image(). Sebelumnya
    logika ini digabung jadi satu fungsi bersama proses download, sehingga
    fitur AI generate harus menduplikasi seluruh logika "cari slot kosong &
    simpan ke DB" ini kalau mau dipakai.
    """
    try:
        from models import Product, db
        product = Product.query.get(product_id)
        if not product:
            logger.error(f"[AdminBot] Produk ID {product_id} tidak ditemukan untuk assign gambar")
            return False

        if not product.image_filename:
            product.image_filename = filename
        elif not product.image_filename2:
            product.image_filename2 = filename
        elif not product.image_filename3:
            product.image_filename3 = filename
        elif not product.image_filename4:
            product.image_filename4 = filename
        else:
            logger.warning(f"[AdminBot] Tidak ada slot gambar kosong untuk produk {product_id}")
            return False

        db.session.commit()
        logger.info(f"[AdminBot] Gambar '{filename}' ditempelkan ke produk {product_id}")
        return True
    except Exception as e:
        logger.error(f"[AdminBot] Gagal assign gambar ke produk {product_id}: {e}")
        try:
            from models import db
            db.session.rollback()
        except Exception:
            pass
        return False

def _add_photo_to_product(product_id: int, file_id: str) -> bool:
    """Unduh foto dari Telegram, lalu tempelkan ke slot gambar kosong produk yang sudah ada."""
    try:
        from models import Product
        product = Product.query.get(product_id)
        if not product:
            logger.error(f"[AdminBot] Produk ID {product_id} tidak ditemukan untuk tambah foto")
            return False

        current_images = [
            product.image_filename,
            product.image_filename2,
            product.image_filename3,
            product.image_filename4
        ]
        if all(current_images):
            logger.warning(f"[AdminBot] Produk ID {product_id} sudah memiliki 4 foto")
            return False
    except Exception as e:
        logger.error(f"[AdminBot] Gagal cek slot gambar produk {product_id}: {e}")
        return False

    saved_path = _download_telegram_file(file_id)
    if not saved_path:
        logger.error(f"[AdminBot] Gagal mengunduh file untuk produk {product_id}")
        return False

    return _assign_image_to_product(product_id, saved_path)

def _get_product_list(limit: int = 10) -> List[Any]:
    try:
        from models import Product
        return Product.query.order_by(Product.created_at.desc()).limit(limit).all()
    except Exception as e:
        logger.warning(f"[AdminBot] Gagal ambil list produk: {e}")
        return []

def _get_product_by_id(product_id: int) -> Optional[Any]:
    try:
        from models import Product
        return Product.query.get(product_id)
    except Exception:
        return None

def _delete_product(product_id: int) -> bool:
    try:
        from models import Product, db
        product = Product.query.get(product_id)
        if product:
            db.session.delete(product)
            db.session.commit()
            return True
        return False
    except Exception as e:
        logger.error(f"[AdminBot] Gagal hapus produk: {e}")
        try:
            from models import db
            db.session.rollback()
        except Exception:
            pass
        return False

def _update_product(product_id: int, updates: Dict[str, Any]) -> bool:
    try:
        from models import Product, db
        product = Product.query.get(product_id)
        if not product:
            return False
        for key, value in updates.items():
            if hasattr(product, key):
                setattr(product, key, value)
        product.updated_at = datetime.utcnow()
        db.session.commit()
        return True
    except Exception as e:
        logger.error(f"[AdminBot] Gagal update produk: {e}")
        try:
            from models import db
            db.session.rollback()
        except Exception:
            pass
        return False

# ------------------------------------------------------------------------------
# SESSION MANAGER — Wizard Posting Produk (Step-by-step)
# ------------------------------------------------------------------------------
class ProductWizardSession:
    _sessions = {}
    _TTL = 30 * 60  # 30 menit

    STEPS = [
        'brand',
        'model',
        'specifications',
        'condition',
        'price',
        'compare_at_price',
        'stock',
        'category',
        'description',
        'images',
        'confirm'
    ]

    @classmethod
    def _purge_expired(cls):
        now = time.time()
        expired = [cid for cid, s in cls._sessions.items() if now - s['last_active'] > cls._TTL]
        for cid in expired:
            cls._sessions.pop(cid, None)

    @classmethod
    def get(cls, chat_id: int) -> Optional[Dict]:
        cls._purge_expired()
        return cls._sessions.get(chat_id)

    @classmethod
    def create(cls, chat_id: int) -> Dict:
        cls._sessions[chat_id] = {
            'step': 0,
            'data': {},
            'last_active': time.time(),
            'ai_suggestions': {},
            'menu_message_id': None,
        }
        return cls._sessions[chat_id]

    @classmethod
    def update(cls, chat_id: int, key: str, value: Any) -> Optional[Dict]:
        session = cls.get(chat_id)
        if session:
            session['data'][key] = value
            session['last_active'] = time.time()
        return session

    @classmethod
    def next_step(cls, chat_id: int) -> Optional[Dict]:
        session = cls.get(chat_id)
        if session:
            session['step'] = min(session['step'] + 1, len(cls.STEPS) - 1)
            session['last_active'] = time.time()
        return session

    @classmethod
    def prev_step(cls, chat_id: int) -> Optional[Dict]:
        session = cls.get(chat_id)
        if session:
            session['step'] = max(session['step'] - 1, 0)
            session['last_active'] = time.time()
        return session

    @classmethod
    def delete(cls, chat_id: int):
        cls._sessions.pop(chat_id, None)

# ------------------------------------------------------------------------------
# WIZARD STEP HANDLERS (Step-by-step) — teks + keyboard inline
# ------------------------------------------------------------------------------
# Sentinel untuk membedakan "field belum pernah diisi" dari "field sudah
# diisi None secara eksplisit" (mis. user menekan tombol "Lewati").
_SPECS_NOT_SET = object()

def _wizard_prompt_for_step(step_index: int, session_data: Dict[str, Any]) -> str:
    steps = ProductWizardSession.STEPS
    step = steps[step_index]

    if step == 'brand':
        return (
            "<b>📝 POSTING PRODUK BARU</b>\n\n"
            "<b>Langkah 1/11 — Brand</b>\n"
            "Masukkan brand HP (contoh: iPhone, Samsung, Xiaomi, Oppo):"
        )
    elif step == 'model':
        return (
            f"<b>Langkah 2/11 — Model</b>\n"
            f"Brand: <code>{_escape_html(session_data.get('brand', '-'))}</code>\n\n"
            f"Masukkan model/nama lengkap HP (contoh: iPhone 13 Pro Max 256GB):"
        )
    elif step == 'specifications':
        brand = session_data.get('brand', '')
        model = session_data.get('model', '')

        # Generate otomatis sekali saja per kunjungan step (di-cache di session_data),
        # tombol "Generate Ulang" akan mereset flag ini.
        if not session_data.get('_specs_generated'):
            debug: Dict[str, Any] = {}
            specs = _generate_product_specs(brand, model, debug=debug)
            session_data['_ai_specs_draft'] = specs
            session_data['_specs_generated'] = True
            session_data['_specs_debug'] = debug

        specs = session_data.get('_ai_specs_draft')
        debug = session_data.get('_specs_debug') or {}
        header = (
            f"<b>Langkah 3/11 — Spesifikasi Produk (AI)</b>\n"
            f"Brand: <code>{_escape_html(brand)}</code> | Model: <code>{_escape_html(model)}</code>\n\n"
        )
        if specs:
            # BUGFIX: nilai spesifikasi berasal dari respons bebas AI dan bisa memuat
            # karakter '&'/'<'/'>' (mis. "5G & LTE", "<1 tahun"). Tanpa escape ini,
            # sendMessage dengan parse_mode HTML gagal secara DIAM-DIAM (Telegram
            # menolak, _send_message menangkap exception dan mengembalikan None),
            # sehingga admin tidak pernah melihat hasil spesifikasi AI di step ini.
            specs_list = "\n".join(
                f"• <b>{_escape_html(k.replace('_', ' ').title())}:</b> {_escape_html(str(v))}"
                for k, v in specs.items()
            )
            note_line = f"\n<i>Catatan: {_escape_html(debug['note'])}</i>\n" if debug.get('note') else ""
            body = (
                f"🤖 AI berhasil mengisi spesifikasi lengkap secara otomatis:\n\n"
                f"{specs_list}\n{note_line}\n"
                f"Tekan <b>Gunakan</b> untuk lanjut, <b>Generate Ulang</b> jika kurang sesuai, "
                f"atau ketik teks manual untuk menambahkan catatan spesifikasi."
            )
        else:
            reason = debug.get('error') or 'Penyebab tidak diketahui (tidak ada detail dari AI).'
            raw_snippet = (debug.get('raw') or '').strip()
            raw_block = ""
            if raw_snippet:
                raw_block = (
                    f"\n<b>Respons mentah AI (debug, dipotong 400 karakter):</b>\n"
                    f"<code>{_escape_html(raw_snippet[:400])}</code>\n"
                )
            body = (
                f"⚠️ AI belum berhasil menghasilkan spesifikasi otomatis.\n"
                f"<b>Alasan:</b> {_escape_html(reason)}\n"
                f"{raw_block}\n"
                "Tekan <b>Generate Ulang</b> untuk mencoba lagi, <b>Lewati</b> untuk melanjutkan "
                "tanpa spesifikasi, atau ketik spesifikasi secara manual."
            )
        return header + body
    elif step == 'condition':
        return (
            f"<b>Langkah 4/11 — Kondisi</b>\n"
            f"Brand: <code>{_escape_html(session_data.get('brand', '-'))}</code>\n"
            f"Model: <code>{_escape_html(session_data.get('model', '-'))}</code>\n\n"
            f"Pilih kondisi dari tombol di bawah, atau ketik manual:"
        )
    elif step == 'price':
        brand = session_data.get('brand', '')
        model = session_data.get('model', '')
        condition = session_data.get('condition', '')
        # BUGFIX: sebelumnya _suggest_price() dipanggil TANPA CACHE setiap kali step
        # ini di-render — termasuk saat admin menekan tombol "⬅️ Kembali" dari step
        # berikutnya lalu maju lagi. Ini berarti setiap kunjungan berulang ke step
        # harga memicu panggilan API OpenRouter baru (biaya & latensi tambahan yang
        # tidak perlu), padahal brand/model/kondisi belum berubah. Sekarang di-cache
        # per kombinasi brand+model+kondisi, sama seperti pola caching di step
        # 'specifications'; hanya generate ulang kalau salah satu berubah.
        cache_key = (brand, model, condition)
        if session_data.get('_price_suggestion_key') != cache_key:
            suggested = _suggest_price(brand, model, condition)
            session_data['_ai_price_suggestion'] = suggested
            session_data['_price_suggestion_key'] = cache_key
        suggested = session_data.get('_ai_price_suggestion')
        price_text = f"💡 <i>AI sarankan harga: {_format_price(suggested)}</i>\n" if suggested else ""
        return (
            f"<b>Langkah 5/11 — Harga Jual</b>\n"
            f"Brand: <code>{_escape_html(brand)}</code> | Model: <code>{_escape_html(model)}</code>\n"
            f"Kondisi: <code>{_escape_html(condition)}</code>\n\n"
            f"{price_text}"
            f"Masukkan harga jual dalam Rupiah (angka saja, contoh: 2500000):"
        )
    elif step == 'compare_at_price':
        brand = session_data.get('brand', '')
        model = session_data.get('model', '')
        price = session_data.get('price', 0)
        return (
            f"<b>Langkah 6/11 — Harga Coret (Opsional)</b>\n"
            f"Brand: <code>{_escape_html(brand)}</code> | Model: <code>{_escape_html(model)}</code>\n"
            f"Harga jual: <code>{_format_price(price)}</code>\n\n"
            f"Masukkan harga asli (coret) dalam Rupiah, atau tekan tombol Skip:\n"
            f"(contoh: 3000000)"
        )
    elif step == 'stock':
        return (
            f"<b>Langkah 7/11 — Stok</b>\n"
            f"Harga jual: <code>{_format_price(session_data.get('price', 0))}</code>\n"
            f"Harga coret: {_format_price(session_data.get('compare_at_price')) if session_data.get('compare_at_price') else '—'}\n\n"
            f"Pilih stok cepat atau masukkan jumlah stok (angka):"
        )
    elif step == 'category':
        return (
            f"<b>Langkah 8/11 — Kategori</b>\n\n"
            f"Pilih kategori dari tombol, atau ketik sendiri:"
        )
    elif step == 'description':
        brand = session_data.get('brand', '')
        model = session_data.get('model', '')
        return (
            f"<b>Langkah 9/11 — Deskripsi Produk (Opsional)</b>\n"
            f"Brand: <code>{_escape_html(brand)}</code> | Model: <code>{_escape_html(model)}</code>\n\n"
            f"Masukkan deskripsi produk, atau tekan tombol untuk pakai AI generate otomatis:"
        )
    elif step == 'images':
        return (
            f"<b>Langkah 10/11 — Gambar Produk</b>\n\n"
            f"Kirimkan foto produk asli (bisa lebih dari 1), atau tekan <b>Generate Foto AI</b> "
            f"untuk membuat gambar produk otomatis dengan AI.\n"
            f"Bot akan menyimpan hingga 4 gambar.\n\n"
            f"Foto saat ini: {session_data.get('_image_count', 0)}/4\n"
            f"Tekan <b>Selesai</b> setelah semua foto siap, atau <b>Skip</b> jika tidak ada foto."
        )
    elif step == 'confirm':
        brand = session_data.get('brand', '')
        model = session_data.get('model', '')
        condition = session_data.get('condition', '')
        price = session_data.get('price', 0)
        compare_at_price = session_data.get('compare_at_price')
        description_manual = session_data.get('description')

        ai_name = _generate_product_name(brand, model, condition)
        if not description_manual:
            ai_desc = _generate_product_description(brand, model, condition, price // 100)
            if not ai_desc:
                ai_desc = _generate_fallback_description(brand, model, condition, price // 100)
        else:
            ai_desc = description_manual

        # Pakai spesifikasi yang sudah dipilih di step 'specifications'; hanya generate
        # ulang jika step tersebut memang belum pernah dikunjungi (mis. via fastpost /
        # sesi lama), BUKAN jika user sudah eksplisit menekan "Lewati" (nilainya None
        # dengan sengaja, jadi harus tetap dihormati di sini).
        ai_specs = session_data.get('specifications', _SPECS_NOT_SET)
        if ai_specs is _SPECS_NOT_SET:
            ai_specs = _generate_product_specs(brand, model)

        session_data['ai_name'] = ai_name
        session_data['ai_description'] = ai_desc
        session_data['ai_specifications'] = ai_specs

        specs_text = ""
        if ai_specs:
            specs_text = "\n<b>Spesifikasi:</b>\n"
            for k, v in ai_specs.items():
                specs_text += f"  • {_escape_html(k.replace('_', ' ').title())}: {_escape_html(str(v))}\n"

        price_display = f"{_format_price(price)}"
        if compare_at_price:
            price_display = f"<s>{_format_price(compare_at_price)}</s> {price_display}"

        image_count = session_data.get('_image_count', 0)
        image_text = f"{image_count} gambar" if image_count > 0 else "Tidak ada gambar"

        # BUGFIX: ai_name dan ai_desc berasal dari respons bebas AI (dan brand/model/
        # kondisi/kategori dari input admin) — semuanya BISA memuat karakter HTML
        # khusus ('&', '<', '>'), mis. deskripsi yang menyebut "garansi & original"
        # atau "RAM <8GB tersedia>". Tanpa escape, ini membuat sendMessage(parse_mode
        # HTML) gagal DIAM-DIAM sehingga admin tidak pernah melihat layar konfirmasi
        # akhir — kemungkinan cukup sering terjadi karena deskripsi AI panjang.
        return (
            f"<b>Langkah 11/11 — Konfirmasi Posting</b>\n\n"
            f"<b>📱 {_escape_html(ai_name)}</b>\n"
            f"Brand: <code>{_escape_html(brand)}</code>\n"
            f"Model: <code>{_escape_html(model)}</code>\n"
            f"Kondisi: <code>{_escape_html(condition)}</code>\n"
            f"Harga: <code>{price_display}</code>\n"
            f"Stok: <code>{session_data.get('stock', 1)}</code>\n"
            f"Kategori: <code>{_escape_html(session_data.get('category', 'Smartphone'))}</code>\n"
            f"Gambar: <code>{image_text}</code>\n"
            f"{specs_text}\n"
            f"<b>Deskripsi:</b>\n<i>{_escape_html(ai_desc) if ai_desc else '—'}</i>\n\n"
            f"Tekan tombol di bawah untuk melanjutkan:"
        )
    return "❌ Step tidak dikenali."

def _wizard_keyboard_for_step(step_index: int, session_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Kembalikan inline keyboard yang sesuai untuk step wizard saat ini (None jika tidak perlu)."""
    steps = ProductWizardSession.STEPS
    step = steps[step_index]
    if step == 'specifications':
        return _specifications_keyboard(bool(session_data.get('_ai_specs_draft')))
    elif step == 'condition':
        return _condition_keyboard()
    elif step == 'compare_at_price':
        return _compare_price_keyboard()
    elif step == 'stock':
        return _stock_keyboard()
    elif step == 'category':
        return _category_keyboard()
    elif step == 'description':
        return _description_keyboard()
    elif step == 'images':
        return _images_keyboard(session_data.get('_image_count', 0))
    elif step == 'confirm':
        return _confirm_keyboard()
    return None

def _send_wizard_step(chat_id: int, step_index: int, session_data: Dict[str, Any]):
    """Kirim prompt step wizard beserta keyboard yang sesuai, simpan message_id untuk step yang mendukung edit."""
    text = _wizard_prompt_for_step(step_index, session_data)
    keyboard = _wizard_keyboard_for_step(step_index, session_data)
    message_id = _send_message(chat_id, text, reply_markup=keyboard)
    session = ProductWizardSession.get(chat_id)
    if session is not None:
        session['menu_message_id'] = message_id

def _handle_wizard_input(chat_id: int, text: str, session: Dict[str, Any]) -> str:
    steps = ProductWizardSession.STEPS
    step_index = session['step']
    step = steps[step_index]
    data = session['data']

    if step == 'brand':
        if len(text) < 2:
            return "❌ Brand terlalu pendek. Masukkan ulang:"
        data['brand'] = text.title().strip()
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'model':
        if len(text) < 3:
            return "❌ Model terlalu pendek. Masukkan ulang:"
        data['model'] = text.strip()
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'specifications':
        text_lower = text.lower().strip()
        if text_lower in ('lewati', 'skip'):
            data['specifications'] = None
            data.pop('_ai_specs_draft', None)
            data.pop('_specs_generated', None)
            data.pop('_specs_debug', None)
            ProductWizardSession.next_step(chat_id)
            return _wizard_prompt_for_step(session['step'], data)
        if text_lower in ('ulang', 'regenerate', 'generate ulang'):
            data.pop('_ai_specs_draft', None)
            data.pop('_specs_debug', None)
            data['_specs_generated'] = False
            # Render ulang step yang sama (akan otomatis generate ulang di dalamnya)
            return _wizard_prompt_for_step(step_index, data)
        if text_lower in ('gunakan', 'ok', 'ya', 'terima'):
            data['specifications'] = data.get('_ai_specs_draft')
            ProductWizardSession.next_step(chat_id)
            return _wizard_prompt_for_step(session['step'], data)
        # Input manual: gabungkan sebagai catatan tambahan pada spesifikasi AI (jika ada)
        manual_specs = dict(data.get('_ai_specs_draft') or {})
        manual_specs['catatan_tambahan'] = text.strip()
        data['specifications'] = manual_specs
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'condition':
        valid_conditions = ['like new', 'excellent', 'good', 'fair', 'baru', 'bekas', 'second']
        text_lower = text.lower().strip()
        if text_lower not in valid_conditions and len(text) < 3:
            return "❌ Kondisi tidak valid. Pilih dari tombol atau ketik kondisi yang sesuai:"
        data['condition'] = text.title().strip()
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'price':
        clean = re.sub(r'[^\d.]', '', text.replace(',', ''))
        try:
            rm = float(clean)
            data['price'] = int(rm * 100)
        except ValueError:
            return "❌ Format harga tidak valid. Masukkan angka saja (contoh: 2500):"
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'compare_at_price':
        if text.lower().strip() == 'skip':
            data['compare_at_price'] = None
        else:
            clean = re.sub(r'[^\d.]', '', text.replace(',', ''))
            try:
                rm = float(clean)
                data['compare_at_price'] = int(rm * 100)
            except ValueError:
                return "❌ Format harga tidak valid. Masukkan angka saja atau tekan tombol Skip:"
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'stock':
        if text.lower().strip() == 'skip':
            data['stock'] = 1
        else:
            try:
                data['stock'] = max(1, int(text.strip()))
            except ValueError:
                return "❌ Stok harus angka. Masukkan ulang:"
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'category':
        data['category'] = text.title().strip() or 'Smartphone'
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'description':
        if text.strip() == '' or text.lower().strip() == 'skip':
            data['description'] = None
        else:
            data['description'] = text.strip()
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    elif step == 'images':
        if text.lower().strip() == 'skip':
            data['image_filenames'] = []
            data['_image_count'] = 0
            ProductWizardSession.next_step(chat_id)
            return _wizard_prompt_for_step(session['step'], data)
        elif text.lower().strip() == 'selesai':
            if data.get('_image_count', 0) > 0:
                ProductWizardSession.next_step(chat_id)
                return _wizard_prompt_for_step(session['step'], data)
            else:
                return "📷 Belum ada gambar. Kirim foto atau tekan tombol Skip."
        else:
            return (
                "📷 Silakan kirim foto produk sebagai gambar (bukan teks).\n"
                "Atau tekan tombol Skip / Selesai di bawah."
            )

    elif step == 'confirm':
        text_lower = text.lower().strip()
        if text_lower in ('ya', 'yes', 'y', 'post', 'posting'):
            data['name'] = session.get('ai_name') or f"{data.get('brand', '')} {data.get('model', '')} {data.get('condition', '')}".strip()
            data['description'] = session.get('ai_description', '')
            data['specifications'] = session.get('ai_specifications')
            data['short_description'] = (data.get('description', '') or '')[:200]
            product = _create_product_from_data(data)
            if product:
                ProductWizardSession.delete(chat_id)
                return (
                    f"✅ <b>Produk berhasil diposting!</b>\n\n"
                    f"📱 <b>{_escape_html(product.name)}</b>\n"
                    f"💰 Harga: {_format_price(product.price)}\n"
                    f"📦 Stok: {product.stock}\n"
                    f"🔗 ID: <code>{product.id}</code>\n\n"
                    f"Produk sudah aktif di katalog."
                )
            return "❌ Gagal memposting produk. Coba lagi nanti."

        elif text_lower in ('tidak', 'no', 'n', 'batal', 'cancel'):
            ProductWizardSession.delete(chat_id)
            return "❌ Posting produk dibatalkan."

        elif text_lower in ('ulang', 'restart', 'reset'):
            ProductWizardSession.create(chat_id)
            return _wizard_prompt_for_step(0, {})

        return "❓ Tekan tombol Ya, Batal, atau Ulang:"

    return "❌ Terjadi kesalahan. Ketik /postproduk untuk memulai ulang."

def _handle_wizard_ai_image(chat_id: int, session: Dict[str, Any]) -> str:
    """
    Generate gambar produk otomatis dengan AI selama wizard, SEBELUM produk
    dibuat di database. Karena belum ada Product row untuk ditempeli
    (_assign_image_to_product butuh product_id yang sudah ada), gambar yang
    berhasil dibuat cukup ditambahkan ke session_data['image_filenames'] —
    sama seperti alur upload foto manual — dan baru benar-benar terpasang ke
    kolom image_filename produk saat _create_product_from_data() dipanggil
    di step 'confirm'.
    """
    session_index = session['step']
    if ProductWizardSession.STEPS[session_index] != 'images':
        return "🤖 Wizard tidak sedang di step gambar."

    data = session['data']
    image_filenames = data.get('image_filenames', [])
    if len(image_filenames) >= 4:
        return "📷 Maksimal 4 gambar sudah terkumpul. Tekan tombol Selesai untuk lanjut."

    brand = data.get('brand', '')
    model = data.get('model', '')
    condition = data.get('condition', '')

    debug: Dict[str, Any] = {}
    result = _generate_product_image(brand, model, condition, debug=debug)
    if not result:
        reason = debug.get('error') or 'Penyebab tidak diketahui (tidak ada detail dari AI).'
        return (
            f"❌ AI gagal membuat gambar produk.\n"
            f"<b>Alasan:</b> {_escape_html(reason)}\n\n"
            f"Coba tekan <b>Generate Foto AI</b> lagi, kirim foto asli, atau tekan Skip."
        )

    filename = _save_image_bytes(
        result['data'], result.get('mime_type', 'image/png'), filename_prefix='ai_wizard'
    )
    if not filename:
        return "❌ Gambar berhasil dibuat oleh AI tapi gagal disimpan ke disk. Coba lagi."

    image_filenames.append(filename)
    data['image_filenames'] = image_filenames
    data['_image_count'] = len(image_filenames)
    ProductWizardSession.update(chat_id, 'image_filenames', image_filenames)

    if len(image_filenames) >= 4:
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    return (
        f"🤖 Gambar AI berhasil dibuat dan ditambahkan ({len(image_filenames)}/4).\n"
        f"Kirim foto asli, generate lagi dengan AI, atau tekan tombol Selesai untuk lanjut."
    )

def _handle_wizard_photo(chat_id: int, photo_file_id: str, session: Dict[str, Any]) -> str:
    step_index = session['step']
    if ProductWizardSession.STEPS[step_index] != 'images':
        return "📷 Foto diterima tapi wizard tidak sedang meminta gambar."

    data = session['data']
    image_filenames = data.get('image_filenames', [])
    if len(image_filenames) >= 4:
        return "📷 Maksimal 4 gambar sudah terkirim. Tekan tombol Selesai untuk lanjut."

    saved_path = _download_telegram_file(photo_file_id)
    if not saved_path:
        return "❌ Gagal mengunduh foto. Coba kirim ulang."

    image_filenames.append(saved_path)
    data['image_filenames'] = image_filenames
    data['_image_count'] = len(image_filenames)

    ProductWizardSession.update(chat_id, 'image_filenames', image_filenames)
    session['data']['_image_count'] = len(image_filenames)

    if len(image_filenames) >= 4:
        ProductWizardSession.next_step(chat_id)
        return _wizard_prompt_for_step(session['step'], data)

    return (
        f"📷 Gambar diterima ({len(image_filenames)}/4).\n"
        f"Kirim gambar lagi, atau tekan tombol Selesai untuk lanjut."
    )

# ------------------------------------------------------------------------------
# COMMAND HANDLERS
# ------------------------------------------------------------------------------
def _cmd_postproduk(chat_id: int) -> str:
    ProductWizardSession.create(chat_id)
    return _wizard_prompt_for_step(0, {})

def _cmd_fastpost(chat_id: int, args: str) -> str:
    """Mode cepat: /fastpost Nama|Brand|Model|Kondisi|HargaJual|HargaCoret|Stok|Kategori|Deskripsi"""
    if not args:
        return (
            "<b>⚡ MODE CEPAT</b>\n\n"
            "Format: <code>/fastpost Nama|Brand|Model|Kondisi|HargaJual|HargaCoret|Stok|Kategori|Deskripsi</code>\n\n"
            "Semua field opsional kecuali Brand.\n"
            "Contoh: <code>/fastpost iPhone 13 Pro|Apple|13 Pro Max 256GB|Like New|2500|3000|5|Smartphone|Unit mulus, fullset</code>\n\n"
            "Jika nama kosong → AI generate.\n"
            "Jika deskripsi kosong → AI generate.\n"
            "Foto dapat ditambahkan nanti dengan <code>/addphoto &lt;id&gt;</code>."
        )
    parts = [p.strip() for p in args.split('|')]
    data = {
        'name': parts[0] if len(parts) > 0 else '',
        'brand': parts[1] if len(parts) > 1 else '',
        'model': parts[2] if len(parts) > 2 else '',
        'condition': parts[3] if len(parts) > 3 else 'Like New',
        'price': _parse_int(parts[4]) if len(parts) > 4 else None,
        'compare_at_price': _parse_int(parts[5]) if len(parts) > 5 else None,
        'stock': _parse_int(parts[6]) if len(parts) > 6 else 1,
        'category': parts[7] if len(parts) > 7 else 'Smartphone',
        'description': parts[8] if len(parts) > 8 else None,
    }
    if not data['brand']:
        return "❌ Brand wajib diisi. Gunakan format: /fastpost Nama|Brand|..."
    if not data['name']:
        ai_name = _generate_product_name(data['brand'], data['model'], data['condition'])
        data['name'] = ai_name or f"{data['brand']} {data['model']} {data['condition']}"
    if data['price'] is None:
        suggested = _suggest_price(data['brand'], data['model'], data['condition'])
        if suggested:
            data['price'] = suggested
        else:
            return "❌ Harga jual wajib diisi atau AI tidak bisa menyarankan. Masukkan angka."
    if not data['description']:
        ai_desc = _generate_product_description(data['brand'], data['model'], data['condition'], data['price'] // 100)
        data['description'] = ai_desc or _generate_fallback_description(data['brand'], data['model'], data['condition'], data['price'] // 100)
    specs = _generate_product_specs(data['brand'], data['model'])
    if specs:
        data['specifications'] = specs
    product = _create_product_from_data(data)
    if product:
        return (
            f"✅ <b>Produk berhasil diposting (mode cepat)!</b>\n\n"
            f"📱 <b>{_escape_html(product.name)}</b>\n"
            f"💰 Harga: {_format_price(product.price)}\n"
            f"📦 Stok: {product.stock}\n"
            f"🔗 ID: <code>{product.id}</code>\n\n"
            f"Untuk menambah foto, gunakan <code>/addphoto {product.id}</code> dengan caption pada foto."
        )
    return "❌ Gagal memposting produk. Coba lagi nanti."

def _cmd_addphoto(chat_id: int, args: str) -> str:
    """/addphoto <id> — instruksi untuk kirim foto dengan caption /addphoto <id>"""
    if not args:
        return "📷 Format: <code>/addphoto &lt;id&gt;</code>\nContoh: <code>/addphoto 5</code>\n\nKemudian kirim foto dengan caption yang sama."
    try:
        product_id = int(args.strip().split()[0])
    except ValueError:
        return "❌ ID produk harus angka."
    product = _get_product_by_id(product_id)
    if not product:
        return f"❌ Produk ID <code>{product_id}</code> tidak ditemukan."
    return (
        f"📷 Silakan kirim foto untuk produk <b>{_escape_html(product.name)}</b> (ID {product_id}).\n"
        f"Kirim foto dengan caption <code>/addphoto {product_id}</code>.\n"
        f"Maksimal 4 foto."
    )

def _cmd_genfoto(chat_id: int, args: str) -> str:
    """/genfoto <id> — generate gambar produk otomatis pakai AI dan tempelkan ke produk yang sudah ada."""
    if not args:
        return (
            "🖼️ <b>GENERATE FOTO AI</b>\n\n"
            "Format: <code>/genfoto &lt;id&gt;</code>\n"
            "Contoh: <code>/genfoto 5</code>\n\n"
            "Gambar dibuat otomatis berdasarkan brand + model + kondisi produk, "
            "lalu ditempel ke slot foto kosong pertama."
        )
    try:
        product_id = int(args.strip().split()[0])
    except ValueError:
        return "❌ ID produk harus angka."

    product = _get_product_by_id(product_id)
    if not product:
        return f"❌ Produk ID <code>{product_id}</code> tidak ditemukan."

    current_images = [
        product.image_filename, product.image_filename2,
        product.image_filename3, product.image_filename4,
    ]
    if all(current_images):
        return f"❌ Produk ID {product_id} sudah memiliki 4 foto. Hapus salah satu dulu lewat admin panel."

    _send_message(
        chat_id,
        f"🤖 Sedang membuat gambar produk dengan AI untuk <b>{_escape_html(product.name)}</b>, mohon tunggu (bisa sampai ±1 menit)..."
    )

    debug: Dict[str, Any] = {}
    ok = _generate_and_attach_product_image(
        product_id, product.brand or '', product.model or '', product.condition or '', debug=debug
    )
    if ok:
        return f"✅ Gambar AI berhasil ditambahkan ke produk <b>{_escape_html(product.name)}</b> (ID {product_id})."

    reason = debug.get('error') or 'Penyebab tidak diketahui (tidak ada detail dari AI).'
    return (
        f"❌ Gagal generate gambar AI untuk produk ID {product_id}.\n"
        f"<b>Alasan:</b> {_escape_html(reason)}\n\n"
        f"Anda tetap bisa menambah foto asli dengan <code>/addphoto {product_id}</code>, "
        f"atau coba <code>/genfoto {product_id}</code> lagi."
    )

def _cmd_listproduk(chat_id: int = None):
    """Kirim daftar produk. Jika chat_id diberikan, kirim langsung tiap produk dengan tombol aksi."""
    products = _get_product_list(10)
    if not products:
        if chat_id:
            _send_message(chat_id, "📦 Belum ada produk di database.", reply_markup=_back_to_menu_keyboard())
            return None
        return "📦 Belum ada produk di database."

    header = "<b>📦 DAFTAR PRODUK TERBARU</b>\n\nGunakan tombol di bawah tiap produk untuk aksi cepat."
    if chat_id:
        _send_message(chat_id, header)
        for p in products:
            status_icon = "✅" if p.is_active else "❌"
            stock_icon = "🔴" if (p.stock or 0) == 0 else "🟡" if (p.stock or 0) <= 2 else "🟢"
            price_display = _format_price(p.price)
            if p.compare_at_price:
                price_display = f"<s>{_format_price(p.compare_at_price)}</s> {price_display}"
            row_text = (
                f"{status_icon} <b>ID {p.id}</b> | {_escape_html(p.name[:50])}\n"
                f"💰 {price_display} | {stock_icon} Stok: {p.stock or 0} | {_escape_html(p.condition or '-')}"
            )
            _send_message(chat_id, row_text, reply_markup=_product_row_keyboard(p.id))
        _send_message(chat_id, "Selesai menampilkan daftar produk.", reply_markup=_back_to_menu_keyboard())
        return None

    # fallback teks (dipakai bila dipanggil tanpa chat_id)
    lines = ["<b>📦 DAFTAR PRODUK TERBARU</b>\n"]
    for p in products:
        status_icon = "✅" if p.is_active else "❌"
        stock_icon = "🔴" if (p.stock or 0) == 0 else "🟡" if (p.stock or 0) <= 2 else "🟢"
        price_display = _format_price(p.price)
        if p.compare_at_price:
            price_display = f"<s>{_format_price(p.compare_at_price)}</s> {price_display}"
        lines.append(
            f"{status_icon} <b>ID {p.id}</b> | {_escape_html(p.name[:40])}\n"
            f"   💰 {price_display} | {stock_icon} Stok: {p.stock or 0} | Kondisi: {_escape_html(p.condition or '-')}\n"
        )
    lines.append("\n<i>Gunakan /editproduk &lt;id&gt; atau /hapusproduk &lt;id&gt;</i>")
    return "\n".join(lines)

def _cmd_editproduk(args: str) -> str:
    if not args:
        return "📝 Format: /editproduk &lt;id&gt;\nContoh: /editproduk 5"
    try:
        product_id = int(args.strip().split()[0])
    except ValueError:
        return "❌ ID produk harus angka."
    product = _get_product_by_id(product_id)
    if not product:
        return f"❌ Produk ID <code>{product_id}</code> tidak ditemukan."
    price_display = _format_price(product.price)
    if product.compare_at_price:
        price_display = f"<s>{_format_price(product.compare_at_price)}</s> {price_display}"
    return (
        f"<b>✏️ EDIT PRODUK ID {product_id}</b>\n\n"
        f"Nama: <code>{_escape_html(product.name)}</code>\n"
        f"Brand: <code>{_escape_html(product.brand or '-')}</code>\n"
        f"Model: <code>{_escape_html(product.model or '-')}</code>\n"
        f"Harga: <code>{price_display}</code>\n"
        f"Stok: <code>{product.stock or 0}</code>\n"
        f"Kondisi: <code>{_escape_html(product.condition or '-')}</code>\n"
        f"Status: <code>{'Aktif' if product.is_active else 'Nonaktif'}</code>\n\n"
        f"<i>Untuk mengedit detail, gunakan admin panel web.</i>"
    )

def _cmd_hapusproduk(chat_id: int, args: str) -> str:
    if not args:
        return "🗑️ Format: /hapusproduk &lt;id&gt;\nContoh: /hapusproduk 5"
    try:
        product_id = int(args.strip().split()[0])
    except ValueError:
        return "❌ ID produk harus angka."
    return _send_delete_confirmation(chat_id, product_id)

def _send_delete_confirmation(chat_id: int, product_id: int) -> Optional[str]:
    product = _get_product_by_id(product_id)
    if not product:
        _send_message(chat_id, f"❌ Produk ID <code>{product_id}</code> tidak ditemukan.")
        return None
    msg = (
        f"⚠️ <b>Konfirmasi Hapus Produk</b>\n\n"
        f"Anda akan menghapus produk:\n"
        f"<b>{_escape_html(product.name)}</b> (ID {product_id})\n\n"
        f"Apakah Anda yakin?"
    )
    _send_message(chat_id, msg, reply_markup=_delete_confirm_keyboard(product_id))
    return None

def _handle_delete_callback(chat_id: int, callback_query_id: str, data: str) -> str:
    parts = data.split('_')
    if len(parts) != 3:
        return "❌ Data callback tidak valid."
    action = parts[1]
    try:
        product_id = int(parts[2])
    except ValueError:
        return "❌ ID produk tidak valid."
    if action == 'confirm':
        product = _get_product_by_id(product_id)
        if not product:
            return f"❌ Produk ID {product_id} sudah tidak ada."
        name = product.name
        if _delete_product(product_id):
            return f"🗑️ Produk <b>{_escape_html(name)}</b> (ID {product_id}) berhasil dihapus."
        else:
            return f"❌ Gagal menghapus produk ID {product_id}."
    elif action == 'cancel':
        return "❌ Penghapusan dibatalkan."
    else:
        return "❌ Aksi tidak dikenal."

def _cmd_bantuan() -> str:
    return (
        "<b>🤖 BOT ADMIN — MENU</b>\n\n"
        "Pilih menu di bawah, atau ketik perintah manual:\n\n"
        "• /postproduk — Posting produk baru (wizard)\n"
        "• /fastpost — Mode cepat\n"
        "• /addphoto &lt;id&gt; — Tambah foto asli\n"
        "• /genfoto &lt;id&gt; — Generate foto produk otomatis pakai AI\n"
        "• /listproduk — Daftar produk\n"
        "• /editproduk &lt;id&gt; — Detail produk\n"
        "• /hapusproduk &lt;id&gt; — Hapus produk\n"
        "• /ai &lt;brand|model|kondisi&gt; — Generate konten AI\n\n"
        "<i>Wizard posting memiliki timeout 30 menit. Harga disimpan dalam sen.</i>"
    )

def _cmd_ai_generate(chat_id: int, args: str) -> str:
    if not args:
        return (
            "🤖 <b>AI GENERATE</b>\n\n"
            "Format: /ai &lt;brand&gt; | &lt;model&gt; | &lt;kondisi&gt;\n"
            "Contoh: /ai iPhone | 13 Pro Max 256GB | Like New"
        )
    parts = [p.strip() for p in args.split('|')]
    if len(parts) < 2:
        return "❌ Format tidak valid. Gunakan: /ai brand | model | kondisi"
    brand = parts[0]
    model = parts[1] if len(parts) > 1 else ''
    condition = parts[2] if len(parts) > 2 else 'Like New'
    # BUGFIX: sebelumnya dikirim ke _admin_chat_id() yang hardcode dari setting,
    # bukan ke chat_id pemanggil yang sebenarnya. Kalau admin_chat_id belum diset
    # atau berbeda dari chat yang sedang chat dengan bot, pesan "sedang generate"
    # ini hilang / salah tujuan. Sekarang pakai chat_id parameter.
    _send_message(chat_id, "⏳ AI sedang generate konten produk...")
    name = _generate_product_name(brand, model, condition)
    desc = _generate_product_description(brand, model, condition, 0)
    if not desc:
        desc = _generate_fallback_description(brand, model, condition, 0)
    specs = _generate_product_specs(brand, model)
    price = _suggest_price(brand, model, condition)
    specs_text = ""
    if specs:
        specs_text = "\n<b>Spesifikasi:</b>\n"
        for k, v in specs.items():
            specs_text += f"  • {_escape_html(str(k))}: {_escape_html(str(v))}\n"
    return (
        f"<b>🤖 HASIL GENERATE AI</b>\n\n"
        f"<b>Nama:</b> <code>{_escape_html(name or '-')}</code>\n"
        f"<b>Harga AI:</b> <code>{_format_price(price) if price else '—'}</code>\n"
        f"{specs_text}\n"
        f"<b>Deskripsi:</b>\n<i>{_escape_html(desc or '—')}</i>\n\n"
        f"Ketik /postproduk untuk langsung posting dengan data ini."
    )

# ------------------------------------------------------------------------------
# MAIN MESSAGE PROCESSOR
# ------------------------------------------------------------------------------
def process_admin_message(text: str, chat_id: int, sender_name: str, username: str) -> str:
    text = text.strip()
    text_lower = text.lower()

    session = ProductWizardSession.get(chat_id)
    if session:
        reply = _handle_wizard_input(chat_id, text, session)
        # Kirim ulang dengan keyboard yang sesuai untuk step baru (kecuali sudah selesai/dibatalkan)
        session_after = ProductWizardSession.get(chat_id)
        if session_after:
            keyboard = _wizard_keyboard_for_step(session_after['step'], session_after['data'])
            if keyboard:
                _send_message(chat_id, reply, reply_markup=keyboard)
                return None
        else:
            _send_message(chat_id, reply, reply_markup=_back_to_menu_keyboard())
            return None
        return reply

    if text_lower.startswith('/postproduk'):
        ProductWizardSession.create(chat_id)
        _send_wizard_step(chat_id, 0, {})
        return None
    if text_lower.startswith('/fastpost'):
        args = text[len('/fastpost'):].strip()
        return _cmd_fastpost(chat_id, args)
    if text_lower.startswith('/addphoto'):
        args = text[len('/addphoto'):].strip()
        return _cmd_addphoto(chat_id, args)
    if text_lower.startswith('/listproduk'):
        return _cmd_listproduk(chat_id)
    if text_lower.startswith('/editproduk'):
        args = text[len('/editproduk'):].strip()
        return _cmd_editproduk(args)
    if text_lower.startswith('/hapusproduk'):
        args = text[len('/hapusproduk'):].strip()
        return _cmd_hapusproduk(chat_id, args)
    if text_lower.startswith('/genfoto'):
        args = text[len('/genfoto'):].strip()
        return _cmd_genfoto(chat_id, args)
    if text_lower.startswith('/ai'):
        args = text[len('/ai'):].strip()
        return _cmd_ai_generate(chat_id, args)
    if text_lower.startswith('/bantuan') or text_lower.startswith('/help') or text_lower.startswith('/start'):
        _send_message(chat_id, _cmd_bantuan(), reply_markup=_main_menu_keyboard())
        return None

    _send_message(
        chat_id,
        f"👋 <b>Halo {_escape_html(sender_name)}!</b>\n\nSaya adalah bot admin untuk posting produk. Pilih menu di bawah:",
        reply_markup=_main_menu_keyboard()
    )
    return None

def process_admin_photo(photo_file_id: str, chat_id: int, sender_name: str, caption: str = None) -> str:
    """Proses foto yang dikirim admin. Jika caption dimulai dengan /addphoto, tambahkan ke produk."""
    if caption and caption.strip().lower().startswith('/addphoto'):
        args = caption[len('/addphoto'):].strip()
        try:
            product_id = int(args.split()[0])
        except (ValueError, IndexError):
            return "❌ Format caption salah. Gunakan: /addphoto <id>"
        product = _get_product_by_id(product_id)
        if not product:
            return f"❌ Produk ID <code>{product_id}</code> tidak ditemukan."
        if all([product.image_filename, product.image_filename2, product.image_filename3, product.image_filename4]):
            return f"❌ Produk ID {product_id} sudah memiliki 4 foto."
        if _add_photo_to_product(product_id, photo_file_id):
            return f"✅ Foto berhasil ditambahkan ke produk <b>{_escape_html(product.name)}</b> (ID {product_id})."
        else:
            return f"❌ Gagal menambahkan foto ke produk ID {product_id}. Coba kirim ulang dengan format gambar JPG/PNG."

    session = ProductWizardSession.get(chat_id)
    if session:
        reply = _handle_wizard_photo(chat_id, photo_file_id, session)
        session_after = ProductWizardSession.get(chat_id)
        if session_after:
            keyboard = _wizard_keyboard_for_step(session_after['step'], session_after['data'])
            _send_message(chat_id, reply, reply_markup=keyboard)
            return None
        return reply
    return "📷 Foto diterima. Gunakan /postproduk untuk memulai wizard posting produk, atau /addphoto <id> untuk menambah foto ke produk yang sudah ada."

# ------------------------------------------------------------------------------
# CALLBACK QUERY PROCESSOR (tombol inline)
# ------------------------------------------------------------------------------
def _process_menu_callback(chat_id: int, data: str) -> Optional[str]:
    """Proses tombol menu utama: menu:xxx"""
    action = data[len('menu:'):]

    if action == 'postproduk':
        ProductWizardSession.create(chat_id)
        _send_wizard_step(chat_id, 0, {})
        return None
    if action == 'fastpost':
        return _cmd_fastpost(chat_id, '')
    if action == 'listproduk':
        _cmd_listproduk(chat_id)
        return None
    if action == 'ai':
        return _cmd_ai_generate(chat_id, '')
    if action == 'addphoto':
        return _cmd_addphoto(chat_id, '')
    if action == 'genfoto' or action.startswith('genfoto_'):
        pid = action[len('genfoto_'):] if action.startswith('genfoto_') else ''
        return _cmd_genfoto(chat_id, pid)
    if action == 'bantuan':
        _send_message(chat_id, _cmd_bantuan(), reply_markup=_main_menu_keyboard())
        return None
    if action.startswith('edit_'):
        pid = action[len('edit_'):]
        return _cmd_editproduk(pid)
    if action.startswith('addphoto_'):
        pid = action[len('addphoto_'):]
        return _cmd_addphoto(chat_id, pid)

    return "❓ Menu tidak dikenal."

def _process_wizard_callback(chat_id: int, callback_query_id: str, data: str) -> Optional[str]:
    """Proses tombol wizard: wiz:xxx"""
    session = ProductWizardSession.get(chat_id)
    if not session:
        return "⌛ Sesi wizard sudah berakhir. Ketik /postproduk untuk memulai lagi."

    payload = data[len('wiz:'):]

    if payload == 'back':
        ProductWizardSession.prev_step(chat_id)
        reply = _wizard_prompt_for_step(session['step'], session['data'])
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'specs_accept':
        reply = _handle_wizard_input(chat_id, 'gunakan', session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'specs_regenerate':
        reply = _handle_wizard_input(chat_id, 'ulang', session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'specs_skip':
        reply = _handle_wizard_input(chat_id, 'lewati', session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload.startswith('condition:'):
        value = payload[len('condition:'):]
        reply = _handle_wizard_input(chat_id, value, session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload.startswith('category:'):
        value = payload[len('category:'):]
        reply = _handle_wizard_input(chat_id, value, session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload.startswith('stock:'):
        value = payload[len('stock:'):]
        reply = _handle_wizard_input(chat_id, value, session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'compare_skip':
        reply = _handle_wizard_input(chat_id, 'skip', session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'desc_skip':
        reply = _handle_wizard_input(chat_id, 'skip', session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'images_ai':
        reply = _handle_wizard_ai_image(chat_id, session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'images_done':
        reply = _handle_wizard_input(chat_id, 'selesai', session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'images_skip':
        reply = _handle_wizard_input(chat_id, 'skip', session)
        keyboard = _wizard_keyboard_for_step(session['step'], session['data'])
        _send_message(chat_id, reply, reply_markup=keyboard)
        return None

    if payload == 'confirm_yes':
        reply = _handle_wizard_input(chat_id, 'ya', session)
        _send_message(chat_id, reply, reply_markup=_back_to_menu_keyboard())
        return None

    if payload == 'confirm_no':
        reply = _handle_wizard_input(chat_id, 'tidak', session)
        _send_message(chat_id, reply, reply_markup=_back_to_menu_keyboard())
        return None

    if payload == 'confirm_restart':
        ProductWizardSession.create(chat_id)
        _send_wizard_step(chat_id, 0, {})
        return None

    return "❓ Aksi wizard tidak dikenal."

def process_admin_callback(chat_id: int, callback_query_id: str, data: str) -> Optional[str]:
    """Router utama untuk semua callback_query dari tombol inline."""
    if data.startswith('delete_ask_'):
        product_id_str = data[len('delete_ask_'):]
        try:
            product_id = int(product_id_str)
        except ValueError:
            return "❌ ID produk tidak valid."
        return _send_delete_confirmation(chat_id, product_id)

    if data.startswith('delete_'):
        return _handle_delete_callback(chat_id, callback_query_id, data)

    if data.startswith('menu:'):
        return _process_menu_callback(chat_id, data)

    if data.startswith('wiz:'):
        return _process_wizard_callback(chat_id, callback_query_id, data)

    return "❓ Aksi tidak dikenal."

# ------------------------------------------------------------------------------
# FLASK REGISTRATION
# ------------------------------------------------------------------------------
def register_telegram_admin_bot(app):
    """Register webhook endpoint ke aplikasi Flask."""
    if 'telegram_admin_bot_webhook' in app.view_functions:
        logger.warning("register_telegram_admin_bot() dilewati: endpoint sudah terdaftar.")
        return

    @app.route('/webhook/telegram/admin', methods=['POST'], strict_slashes=False)
    def telegram_admin_bot_webhook():
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
        if not _RateLimiter.is_allowed(f"tg_admin_bot:{client_ip}", limit=30, window=60):
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded'}), 429

        configured_secret = _get_setting('telegram_webhook_secret', '')
        if configured_secret:
            secret_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if not hmac.compare_digest(secret_token, configured_secret):
                return Response('Unauthorized', status=401)

        try:
            update = json.loads(request.get_data() or b'{}')
        except json.JSONDecodeError:
            logger.warning("[AdminBot] Invalid JSON")
            return jsonify({'status': 'error', 'message': 'Invalid JSON'}), 400

        try:
            logger.info(f"[AdminBot] Update diterima: {json.dumps(update)[:300]}")
            admin_chat_id = _admin_chat_id()

            if 'message' in update:
                message = update['message'] or {}
                chat = message.get('chat') or {}
                chat_id = chat.get('id')
                chat_type = chat.get('type', '')
                text = message.get('text', '')
                caption = message.get('caption', '')
                sender = message.get('from') or {}
                sender_name = f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip()
                username = sender.get('username', '')

                if admin_chat_id and str(chat_id) != str(admin_chat_id):
                    _send_message(chat_id, "❌ Bot ini hanya untuk admin.")
                    return jsonify({'status': 'ok'})

                if message.get('photo') and chat_type == 'private':
                    photos = message['photo']
                    largest = photos[-1]
                    photo_file_id = largest.get('file_id', '')
                    response_text = process_admin_photo(photo_file_id, chat_id, sender_name, caption)
                    if response_text is not None:
                        _send_message(chat_id, response_text)
                    return jsonify({'status': 'ok'})

                if text and chat_id is not None:
                    response_text = process_admin_message(text, chat_id, sender_name, username)
                    if response_text is not None:
                        _send_message(chat_id, response_text)

            elif 'callback_query' in update:
                callback_query = update['callback_query'] or {}
                callback_query_id = callback_query.get('id')
                data = callback_query.get('data', '')
                cb_message = callback_query.get('message') or {}
                cb_chat = cb_message.get('chat') or {}
                chat_id = cb_chat.get('id')

                if admin_chat_id and str(chat_id) != str(admin_chat_id):
                    _answer_callback(callback_query_id, "Bot ini hanya untuk admin.")
                    return jsonify({'status': 'ok'})

                if not callback_query_id or not chat_id:
                    return jsonify({'status': 'ok'})

                # Selalu jawab callback dulu supaya tombol tidak "loading" terus di app Telegram
                _answer_callback(callback_query_id)

                response_text = process_admin_callback(chat_id, callback_query_id, data)

                if response_text is not None:
                    # Untuk aksi delete, edit pesan lama agar tombol hilang
                    if data.startswith('delete_confirm_') or data.startswith('delete_cancel_'):
                        _edit_message_text(chat_id, cb_message.get('message_id'), response_text)
                    else:
                        _send_message(chat_id, response_text, reply_markup=_back_to_menu_keyboard())

            return jsonify({'status': 'ok'})

        except Exception as e:
            logger.error(f"[AdminBot] Error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Internal error logged'}), 200

    logger.info("Telegram Admin Bot webhook registered at /webhook/telegram/admin")


if __name__ == '__main__':
    print("[INFO] Modul Flask, bukan server standalone.")
    print("Cara pakai: from telegram_admin_bot import register_telegram_admin_bot")