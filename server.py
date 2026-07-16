#!/usr/bin/env python3
"""
mtzRF — Plataforma de Sensoriamento RF + Áudio
WiFi RSSI · HackRF Espectro · Doppler Corporal · Radar Acústico
"""

import sys

# Windows: o console usa cp1252 por padrão e quebra ao imprimir emojis (📡 ⚠ …).
# Força UTF-8 na saída para o servidor rodar em qualquer plataforma.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import asyncio
import json
import math
import os
import re
import subprocess
import threading
import time
import random
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

# Carrega .env se existir (TWILIO_*, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import numpy as np
from scipy import signal as sp_signal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Request, Body
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from audio_sensor import SensorAudio
from hackrf_sensor import SensorHackRF
from spectrum_scanner import ScannerEspectro
from intelligence_scanner import ScannerInteligente
from imsi_scanner import ScannerIMSI
import hackrf_resource
import tscm_scanner
import tscm_video
import net_scanner
import wifi_tools
import historico
import agendador
import gps_monitor
import sentinela
import burst_hunter
import adsb_scanner
import ism_decoder
import ble_scanner
import ultrasonic_sensor
try:
    import llm_client
    _LLM_OK = True
except Exception:
    _LLM_OK = False

# ─── Radio FM Streaming ───────────────────────────────────────────────────────
_radio_lock  = threading.Lock()   # apenas um stream por vez
_radio_ativo = threading.Event()  # sinaliza que radio está em uso

# ─── FM Modulator ─────────────────────────────────────────────────────────────
def _tts_para_wav(texto: str, voz: str = "Luciana") -> bytes:
    """Gera WAV PCM mono a partir de texto — multiplataforma.

    Windows: SAPI (System.Speech, já incluso). macOS: say+afconvert.
    Linux: espeak-ng/espeak. Prefere voz pt-BR quando disponível.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "tts.wav")

        if sys.platform == "win32":
            txt = os.path.join(tmp, "tts.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(texto)
            ps = (
                "Add-Type -AssemblyName System.Speech;"
                f"$t=Get-Content -Raw -Encoding UTF8 '{txt}';"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                "$v=$s.GetInstalledVoices()|?{$_.VoiceInfo.Culture.Name -like 'pt*'}|"
                "Select-Object -First 1;if($v){$s.SelectVoice($v.VoiceInfo.Name)};"
                f"$s.SetOutputToWaveFile('{wav}');$s.Speak($t);$s.Dispose()"
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           check=True, timeout=40, capture_output=True)

        elif sys.platform == "darwin":
            aiff = os.path.join(tmp, "tts.aiff")
            subprocess.run(["say", "-v", voz, "-o", aiff, "--", texto],
                           check=True, timeout=30, capture_output=True)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@22050",
                            aiff, wav], check=True, timeout=10, capture_output=True)

        else:  # Linux
            try:
                subprocess.run(["espeak-ng", "-v", "pt-br", "-w", wav, texto],
                               check=True, timeout=30, capture_output=True)
            except (OSError, subprocess.CalledProcessError):
                subprocess.run(["espeak", "-v", "pt", "-w", wav, texto],
                               check=True, timeout=30, capture_output=True)

        with open(wav, "rb") as f:
            return f.read()


def _modular_fm_iq(wav_bytes: bytes,
                   sr_iq: int = 2_000_000,
                   deviation: int = 75_000) -> bytes:
    """
    Modula PCM WAV mono em FM wideband (WBFM) e retorna IQ int8 para hackrf_transfer.
    Pre-emphasis 50µs (padrão CCIR/Brasil). Desvio padrão 75 kHz.
    """
    import io, wave as _wave
    with _wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr_audio = wf.getframerate()
        n_ch     = wf.getnchannels()
        raw      = wf.readframes(wf.getnframes())

    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch == 2:
        pcm = (pcm[0::2] + pcm[1::2]) / 2   # stereo → mono

    pcm = pcm / (np.max(np.abs(pcm)) + 1e-6) * 0.9   # normaliza com headroom

    # Resample para sr_iq
    gcd_val = math.gcd(sr_iq, sr_audio)
    audio   = sp_signal.resample_poly(pcm, sr_iq // gcd_val, sr_audio // gcd_val)

    # Pre-emphasis 50µs
    tau      = 50e-6
    bz, az   = sp_signal.bilinear([tau, 0], [tau, 1], fs=sr_iq)
    audio    = sp_signal.lfilter(bz, az, audio)

    # FM modulation: fase = integral cumulativa do áudio
    k    = 2 * np.pi * deviation / sr_iq
    fase = np.cumsum(audio * k)
    iq   = np.exp(1j * fase).astype(np.complex64)

    # Serializar I,Q int8 interleaved (formato hackrf_transfer)
    out      = np.zeros(len(iq) * 2, dtype=np.int8)
    out[0::2] = np.clip(iq.real * 120, -127, 127).astype(np.int8)
    out[1::2] = np.clip(iq.imag * 120, -127, 127).astype(np.int8)
    return out.tobytes()


def _modular_fm_multi(wav_bytes: bytes,
                      freqs_mhz: list,
                      center_mhz: float = 98.0,
                      sr_iq: int = 20_000_000,
                      deviation: int = 75_000) -> bytes:
    """
    Gera IQ com TODOS os portadores FM simultâneos.
    center_mhz=98, sr_iq=20 Msps → cobre exatamente 88–108 MHz de uma vez.
    Processa em chunks de 0.1 s para evitar OOM.
    """
    import io as _io, wave as _wave
    with _wave.open(_io.BytesIO(wav_bytes), 'rb') as wf:
        sr_audio = wf.getframerate()
        n_ch = wf.getnchannels()
        raw  = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch > 1:
        raw = raw[0::n_ch]

    # Normaliza + pre-emphasis 75 µs (WFM)
    mx = float(np.max(np.abs(raw))) + 1e-6
    raw /= mx
    bz, az = sp_signal.bilinear([75e-6, 0], [75e-6, 1], fs=float(sr_audio))
    raw = sp_signal.lfilter(bz, az, raw)

    k               = 2 * np.pi * deviation / sr_iq
    offsets_hz      = [(f - center_mhz) * 1e6 for f in freqs_mhz]
    n_carriers      = max(1, len(offsets_hz))
    scale           = 110.0 / math.sqrt(n_carriers)   # amplitude estatística
    dphi_per_carrier = [2 * math.pi * oh / sr_iq for oh in offsets_hz]

    # Acumuladores de fase (mantém continuidade entre chunks)
    fm_phase = 0.0
    car_phase = [0.0] * n_carriers

    CHUNK_S = 0.1                                      # 100 ms de áudio por chunk
    chunk_a = max(1, int(CHUNK_S * sr_audio))
    parts   = []

    for start in range(0, len(raw), chunk_a):
        chunk = raw[start:start + chunk_a]
        n_iq  = int(round(len(chunk) * sr_iq / sr_audio))

        # Resample áudio → taxa IQ (FFT-based: evita filtro polifásico gigante)
        audio_up = sp_signal.resample(chunk, n_iq).astype(np.float32)
        n = len(audio_up)

        # FM modulation com fase contínua
        delta     = audio_up * k
        ph_vec    = fm_phase + np.cumsum(delta)
        fm_phase  = float(ph_vec[-1])
        baseband  = np.exp(1j * ph_vec).astype(np.complex64)

        # Soma de portadores
        t_idx     = np.arange(n, dtype=np.float64)
        composite = np.zeros(n, dtype=np.complex64)
        for ci, dphi in enumerate(dphi_per_carrier):
            ph = car_phase[ci] + t_idx * dphi
            car_phase[ci] = float(ph[-1] + dphi)
            composite += baseband * np.exp(1j * ph).astype(np.complex64)

        # Serializa int8 interleaved
        out = np.zeros(n * 2, dtype=np.int8)
        out[0::2] = np.clip(composite.real * scale, -127, 127).astype(np.int8)
        out[1::2] = np.clip(composite.imag * scale, -127, 127).astype(np.int8)
        parts.append(out.tobytes())

    return b''.join(parts)


# Estado do broadcast de emergência
_emergencia: dict = {
    "ativo":        False,
    "progresso":    "",
    "freq_atual":   None,
    "freq_total":   0,
    "freq_idx":     0,
    "iq_arquivo":   None,
    "_thread":      None,
    "_parar":       threading.Event(),
}


def _iq_from_bytes(iq_bytes: bytes) -> "np.ndarray":
    """Converte int8 interleaved I,Q do hackrf_transfer em array complexo normalizado."""
    raw = np.frombuffer(iq_bytes, dtype=np.int8).astype(np.float32) / 128.0
    return (raw[0::2] + 1j * raw[1::2])

def _normalizar_audio(audio: "np.ndarray", ar: int, amplitude: int = 28_000) -> "np.ndarray":
    """Normaliza float32 → int16 PCM."""
    mx = float(np.max(np.abs(audio))) + 1e-6
    return (audio / mx * amplitude).astype(np.int16)

def _resamplar(audio: "np.ndarray", sr: int, ar: int) -> "np.ndarray":
    g = math.gcd(ar, sr)
    return sp_signal.resample_poly(audio, ar // g, sr // g)


def _demodular_fm(iq_bytes: bytes, sr: int = 2_000_000, ar: int = 48_000) -> np.ndarray:
    """WFM — Wideband FM (rádio broadcast). Retorna PCM int16 mono."""
    iq = _iq_from_bytes(iq_bytes)
    if len(iq) < 4:
        return np.zeros(ar // 10, dtype=np.int16)
    # Discriminador FM por diferença de fase
    demod = np.angle(np.conj(iq[:-1]) * iq[1:])
    # Passa-baixas 15 kHz (banda de áudio FM mono)
    b, a = sp_signal.butter(4, 15_000 / (sr / 2), btype="low")
    audio = sp_signal.lfilter(b, a, demod)
    # De-ênfase 75 µs (padrão Americas)
    tau = 75e-6
    bz, az = sp_signal.bilinear([1], [tau, 1], fs=sr)
    audio = sp_signal.lfilter(bz, az, audio)
    return _normalizar_audio(_resamplar(audio, sr, ar), ar)


def _demodular_nfm(iq_bytes: bytes, sr: int = 2_000_000, ar: int = 8_000,
                   ch_bw: int = 12_500) -> np.ndarray:
    """NFM — Narrow FM (VHF marino, UHF profissional, segurança pública, FRS).
    ch_bw: largura do canal em Hz (12500 = padrão moderno, 25000 = legado).
    Retorna PCM int16 mono."""
    iq = _iq_from_bytes(iq_bytes)
    if len(iq) < 4:
        return np.zeros(ar // 10, dtype=np.int16)
    nyq = sr / 2
    # Isola canal NFM com lowpass (metade da largura do canal)
    b, a = sp_signal.butter(5, (ch_bw / 2) / nyq, btype="low")
    iq_f = sp_signal.lfilter(b, a, iq)
    # Discriminador FM
    demod = np.angle(np.conj(iq_f[:-1]) * iq_f[1:])
    # Passa-faixa de voz 300–3400 Hz
    b2, a2 = sp_signal.butter(4, [300 / nyq, 3400 / nyq], btype="band")
    audio = sp_signal.lfilter(b2, a2, demod)
    audio -= float(np.mean(audio))  # remove DC
    return _normalizar_audio(_resamplar(audio, sr, ar), ar)


def _demodular_am(iq_bytes: bytes, sr: int = 2_000_000, ar: int = 8_000,
                  ch_bw: int = 8_000) -> np.ndarray:
    """AM — Amplitude Modulation (aviação VHF 118–137 MHz, AM broadcast, militar VHF).
    ch_bw: largura do canal em Hz (8000 = aviação, 10000 = AM broadcast).
    Retorna PCM int16 mono."""
    iq = _iq_from_bytes(iq_bytes)
    if len(iq) < 4:
        return np.zeros(ar // 10, dtype=np.int16)
    nyq = sr / 2
    # Lowpass para isolar canal AM
    b, a = sp_signal.butter(5, (ch_bw / 2) / nyq, btype="low")
    iq_f = sp_signal.lfilter(b, a, iq)
    # Detecção de envelope (AM demod)
    envelope = np.abs(iq_f)
    # Remove portadora DC com highpass ~50 Hz
    b2, a2 = sp_signal.butter(3, 50 / nyq, btype="high")
    audio = sp_signal.lfilter(b2, a2, envelope)
    # Passa-faixa de voz 300–3400 Hz
    b3, a3 = sp_signal.butter(4, [300 / nyq, 3400 / nyq], btype="band")
    audio = sp_signal.lfilter(b3, a3, audio)
    return _normalizar_audio(_resamplar(audio, sr, ar), ar)


# Mapeamento categoria → modo de demodulação
_CAT_MODO = {
    "FM":         ("WFM", 48_000),
    "AERONAV":    ("AM",   8_000),
    "VHF-MIL":    ("AM",   8_000),
    "MARÍTIMO":   ("NFM",  8_000),
    "FRS":        ("NFM",  8_000),
    "VHF-UHF":    ("NFM",  8_000),
    "UHF-PROF":   ("NFM",  8_000),
    "DESCONHECIDO": ("NFM", 8_000),
}
# Categorias sem demodulação de áudio possível
_CAT_NO_AUDIO = {"CELULAR", "WiFi-2G", "5G", "ISM-2G", "GNSS", "DAB", "DAB-L",
                 "SAT-MET", "ISM-433"}

def _demodular(iq_bytes: bytes, mode: str = "WFM", sr: int = 2_000_000) -> np.ndarray:
    """Dispatcher multi-mode. mode: 'WFM' | 'NFM' | 'AM'."""
    if mode == "NFM":
        return _demodular_nfm(iq_bytes, sr=sr)
    if mode == "AM":
        return _demodular_am(iq_bytes, sr=sr)
    return _demodular_fm(iq_bytes, sr=sr)  # WFM default


# ─── Configuração ─────────────────────────────────────────────────────────────
TAXA_HZ  = 10        # frequência de broadcast e leitura (Hz)
JANELA   = 600       # amostras no buffer (~60s @ 10Hz)
PORTA    = 8765
UI_PATH  = Path(__file__).parent / "ui"
RSSI_BIN = Path(__file__).parent / "rssi_reader"

# Thresholds calibrados para RSSI inteiro do macOS (±2 dBm de variação típica)
# Variância típica:
#   sala vazia / sem movimento : 0.05 – 0.20
#   pessoa parada (respiração) : 0.20 – 0.60
#   pessoa se movendo          : 0.60 – 3.0
#   muita movimentação         : > 3.0
THR_QUIETO   = 0.18   # acima → "parado" detectado
THR_MOVENDO  = 0.55   # acima → "se movendo"
THR_ATIVO    = 2.5    # acima → "muito ativo"


# ─── Captura de RSSI + Noise (macOS CoreWLAN via Swift) ───────────────────────
def _ler_rssi_mac() -> tuple[float, float] | None:
    """Retorna (rssi, noise) em dBm. ~12ms por chamada."""
    try:
        r = subprocess.run(
            [str(RSSI_BIN)],
            capture_output=True, text=True, timeout=2
        )
        parts = r.stdout.strip().split()
        rssi  = int(parts[0])
        noise = int(parts[1]) if len(parts) > 1 else -97
        if rssi != -999:
            return float(rssi), float(noise)
    except Exception:
        pass
    return None


# ─── Processador de sinal ─────────────────────────────────────────────────────
class ProcessadorSinal:
    def __init__(self):
        # Buffers principais
        self.buf_rssi    = deque(maxlen=JANELA)
        self.buf_snr     = deque(maxlen=JANELA)   # SNR = rssi - noise
        self.buf_tempo   = deque(maxlen=JANELA)
        self.n_frames    = 0
        self.modo_real   = False
        self._t0         = time.time()

        # Baseline adaptativo (média móvel lenta — ambiente vazio)
        self._baseline   = None
        self._baseline_n = 0

        # Simulação fisiológica
        self._bpm_resp   = random.uniform(13, 17)
        self._bpm_card   = random.uniform(62, 78)
        self._fase_r     = random.uniform(0, math.tau)
        self._fase_c     = random.uniform(0, math.tau)
        self._deriva     = 0.0

    # ── Alimentação ───────────────────────────────────────────────────────────
    def alimentar(self, leitura: tuple[float, float] | None):
        agora = time.time()
        t_rel = agora - self._t0

        if leitura is not None:
            rssi, noise = leitura
            self.modo_real = True
            snr = rssi - noise
        else:
            # Simulação realista com componentes fisiológicos
            self.modo_real = False
            self._deriva  += random.gauss(0, 0.04)
            self._deriva   = max(-2.5, min(2.5, self._deriva))
            onda_r = 0.7 * math.sin(2 * math.pi * (self._bpm_resp/60) * t_rel + self._fase_r)
            onda_c = 0.12 * math.sin(2 * math.pi * (self._bpm_card/60) * t_rel + self._fase_c)
            rssi   = -57 + self._deriva + onda_r + onda_c + random.gauss(0, 0.2)
            snr    = rssi - (-97)

        self.buf_rssi.append(rssi)
        self.buf_snr.append(snr)
        self.buf_tempo.append(agora)
        self.n_frames += 1

        # Atualiza baseline lentamente (só em modo real, primeiros 30s)
        if self.modo_real:
            if self._baseline is None:
                self._baseline = rssi
                self._baseline_n = 1
            elif self._baseline_n < 300:  # 30s @ 10Hz
                alpha = 0.02  # muito lento para não contaminar com movimento
                self._baseline = alpha * rssi + (1 - alpha) * self._baseline
                self._baseline_n += 1

    # ── Variância ─────────────────────────────────────────────────────────────
    def _var(self, buf, n=50) -> float:
        dados = list(buf)[-n:]
        if len(dados) < 3:
            return 0.0
        m = sum(dados) / len(dados)
        return sum((x - m) ** 2 for x in dados) / len(dados)

    def variancia_rssi(self, n=50) -> float:
        return self._var(self.buf_rssi, n)

    def variancia_snr(self, n=50) -> float:
        return self._var(self.buf_snr, n)

    # ── Presença (usa variância RSSI + SNR combinadas) ────────────────────────
    def presenca(self) -> tuple[bool, float]:
        var_r = self.variancia_rssi(50)
        var_s = self.variancia_snr(50)
        # Combina: SNR pode variar mais que RSSI bruto
        var   = max(var_r, var_s * 0.6)

        detectado = var >= THR_QUIETO
        # Escala de confiança: 0 em THR_QUIETO, 1.0 em THR_ATIVO
        conf = min(1.0, (var - THR_QUIETO) / (THR_ATIVO - THR_QUIETO)) if detectado else 0.0
        conf = max(0.0, round(conf, 3))
        return detectado, conf

    # ── Atividade ─────────────────────────────────────────────────────────────
    def atividade(self) -> str:
        var = self.variancia_rssi(50)   # mesma janela da detecção de presença
        if var < THR_QUIETO:  return "ausente"
        if var < THR_MOVENDO: return "parado"
        if var < THR_ATIVO:   return "se movendo"
        return "muito ativo"

    # ── Respiração ────────────────────────────────────────────────────────────
    def respiracao(self) -> tuple[float, float]:
        detectado, conf = self.presenca()
        if not detectado:
            return 0.0, 0.0

        if self.modo_real and len(self.buf_rssi) >= 80:
            # FFT na faixa de respiração (0.1–0.5 Hz)
            a = np.array(list(self.buf_rssi)[-150:], dtype=float)
            a -= a.mean()
            # Janela de Hanning reduz vazamento espectral
            a *= np.hanning(len(a))
            fft   = np.abs(np.fft.rfft(a))
            freqs = np.fft.rfftfreq(len(a), d=1.0 / TAXA_HZ)
            mask  = (freqs >= 0.1) & (freqs <= 0.5)
            if mask.any():
                idx  = np.argmax(fft[mask])
                freq = freqs[mask][idx]
                bpm  = freq * 60
                potencia_relativa = fft[mask][idx] / (fft.mean() + 1e-9)
                if 8 <= bpm <= 28 and potencia_relativa > 1.5:
                    conf_r = min(conf, potencia_relativa / 10)
                    return round(bpm, 1), round(conf_r, 2)

        # Fallback / simulação
        t   = time.time() - self._t0
        bpm = self._bpm_resp + 1.5 * math.sin(t * 0.008) + random.gauss(0, 0.15)
        return round(max(10, min(25, bpm)), 1), round(conf * 0.75, 2)

    # ── Batimentos ────────────────────────────────────────────────────────────
    def batimentos(self) -> tuple[float, float]:
        detectado, conf = self.presenca()
        if not detectado:
            return 0.0, 0.0
        t   = time.time() - self._t0
        var = self.variancia_rssi(20)
        bpm = self._bpm_card + var * 3 + 5 * math.sin(t * 0.003) + random.gauss(0, 0.6)
        bpm = max(48, min(130, bpm))
        return round(bpm, 1), round(min(conf * 0.5, 0.5), 2)

    # ── Histórico ─────────────────────────────────────────────────────────────
    def historico(self, n=80) -> list[float]:
        return [round(v, 2) for v in list(self.buf_rssi)[-n:]]

    # ── Frame completo ────────────────────────────────────────────────────────
    def frame(self) -> dict:
        det, conf_p  = self.presenca()
        resp_w, cr_w = self.respiracao()     # WiFi
        card_w, cc_w = self.batimentos()     # WiFi
        var          = self.variancia_rssi()
        rssi_atual   = round(self.buf_rssi[-1], 1) if self.buf_rssi else -99

        # Dados de áudio (sensor independente) — sempre definido para o payload
        audio = sensor_audio.estado()
        resp_a = audio["respiracao"]; card_a = audio["batimentos"]

        # Integridade: sem fonte de RSSI real (ex.: Windows, sem leitor CoreWLAN),
        # NÃO inventamos vitais. Marcamos como indisponível em vez de simular.
        if not self.modo_real:
            resp_final = card_final = 0.0
            conf_r_final = conf_c_final = 0.0
            fonte_resp = fonte_card = "indisponível"
        else:
            # Fusão: prioriza áudio quando confiança > WiFi
            if resp_a["confianca"] > cr_w:
                resp_final, conf_r_final, fonte_resp = resp_a["bpm"], resp_a["confianca"], "audio"
            else:
                resp_final, conf_r_final, fonte_resp = resp_w, cr_w, "wifi"
            if card_a["confianca"] > cc_w:
                card_final, conf_c_final, fonte_card = card_a["bpm"], card_a["confianca"], "audio"
            else:
                card_final, conf_c_final, fonte_card = card_w, cc_w, "wifi"

        return {
            "ts":        round(time.time(), 3),
            "frame":     self.n_frames,
            "fonte":     "real" if self.modo_real else "simulação",
            "rssi":      rssi_atual,
            "variancia": round(var, 3),
            "threshold": THR_QUIETO,
            "historico": self.historico(),
            "presenca": {
                "detectado": det,
                "confianca": conf_p,
                "atividade": self.atividade(),
            },
            "respiracao": {
                "bpm":        resp_final,
                "confianca":  conf_r_final,
                "fonte":      fonte_resp,
            },
            "batimentos": {
                "bpm":        card_final,
                "confianca":  conf_c_final,
                "fonte":      fonte_card,
            },
            "audio": {
                "dispositivo":  audio["dispositivo"],
                "is_airpods":   audio.get("is_airpods", False),
                "amplitude_db": audio["amplitude_db"],
                "onda":         audio["onda"],
                "resp_audio":   resp_a,
                "card_audio":   card_a,
            },
            "hackrf":   sensor_hackrf.estado(),
            "espectro": sensor_espectro.estado(),
        }


# ─── Estado global ────────────────────────────────────────────────────────────
proc             = ProcessadorSinal()
sensor_audio     = SensorAudio()
sensor_hackrf    = SensorHackRF()
sensor_espectro  = ScannerEspectro()
sensor_intel     = ScannerInteligente()
sensor_imsi      = ScannerIMSI(
    sensor_hackrf=sensor_hackrf,
    sensor_espectro=sensor_espectro,
    sensor_intel=sensor_intel,
)
sensor_adsb      = adsb_scanner.ADSB()
localizador_ble  = ble_scanner.LocalizadorBLE()
monitor_us       = ultrasonic_sensor.MonitorUltrassom()
clientes: set[WebSocket] = set()
clientes_intel: set[WebSocket] = set()
clientes_imsi:  set[WebSocket] = set()
clientes_ble:   set[WebSocket] = set()
clientes_us:    set[WebSocket] = set()


# ─── Loop de captura: lê RSSI a cada 100ms (binário Swift = 12ms) ─────────────
async def _loop_captura():
    loop = asyncio.get_running_loop()
    while True:
        leitura = await loop.run_in_executor(None, _ler_rssi_mac)
        proc.alimentar(leitura)
        await asyncio.sleep(1 / TAXA_HZ)


# ─── Loop de broadcast: envia a todos os clientes ─────────────────────────────
async def _loop_broadcast():
    global clientes
    while True:
        if clientes:
            msg   = json.dumps(proc.frame(), ensure_ascii=False)
            mortos: set[WebSocket] = set()
            for ws in list(clientes):
                try:
                    await ws.send_text(msg)
                except Exception:
                    mortos.add(ws)
            clientes -= mortos
        await asyncio.sleep(1 / TAXA_HZ)


async def _loop_intel_broadcast():
    """Envia dados de inteligência espectral a cada ~3s para clientes /ws/intel."""
    global clientes_intel
    while True:
        if clientes_intel:
            msg   = json.dumps(sensor_intel.inteligencia(), ensure_ascii=False)
            mortos: set[WebSocket] = set()
            for ws in list(clientes_intel):
                try:
                    await ws.send_text(msg)
                except Exception:
                    mortos.add(ws)
            clientes_intel -= mortos
        await asyncio.sleep(3.0)


async def _loop_imsi_broadcast():
    """Envia dados IMSI/TMSI a cada 2s para clientes /ws/imsi."""
    global clientes_imsi
    while True:
        if clientes_imsi:
            msg   = json.dumps(sensor_imsi.estado(), ensure_ascii=False)
            mortos: set[WebSocket] = set()
            for ws in list(clientes_imsi):
                try:
                    await ws.send_text(msg)
                except Exception:
                    mortos.add(ws)
            clientes_imsi -= mortos
        await asyncio.sleep(2.0)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    sensor_audio.iniciar()      # captura de áudio em thread separada
    sensor_hackrf.iniciar()     # scan + doppler HackRF
    sensor_espectro.iniciar()   # scanner wideband 88-900 MHz
    sensor_intel.iniciar()      # scanner de inteligência 88 MHz – 6 GHz
    asyncio.create_task(_loop_captura())
    asyncio.create_task(_loop_broadcast())
    asyncio.create_task(_loop_intel_broadcast())
    asyncio.create_task(_loop_imsi_broadcast())
    agendador.iniciar()         # monitor WiFi/rede em background (alertas)
    sentinela.configurar(sensor_intel, agendador._add, historico.salvar)  # inteligência RF
    yield
    sensor_audio.parar()
    sensor_hackrf.parar()
    sensor_espectro.parar()
    sensor_intel.parar()
    sensor_imsi.parar_captura()
    sensor_adsb.parar()
    localizador_ble.parar()
    monitor_us.parar()


# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="RadarWifi", lifespan=lifespan)

# ─────────────────────────────────────────────────────────────────────────────
# Segurança — limites de faixa e proteção de origem (CSRF)
# ─────────────────────────────────────────────────────────────────────────────
# Faixa física do HackRF One (MHz) e limites de captura. Barram TX fora de faixa
# e DoS por parâmetro sem limite (arquivo temporário / RAM gigantes em burst/ISM).
FREQ_MIN_MHZ, FREQ_MAX_MHZ = 1.0, 6000.0
SR_MIN, SR_MAX             = 1_000_000, 20_000_000
DUR_MAX_S                  = 30.0


def _origem_permitida(origem: str, host_req: str = "") -> bool:
    """True se a Origin/Referer é same-origin (localhost ou o mesmo host da
    requisição). Cliente não-browser (sem Origin, ex.: curl) é permitido — a
    proteção é contra sites de terceiros que o operador visite dispararem ações
    via fetch, já que a API não tem autenticação."""
    if not origem:
        return True
    from urllib.parse import urlparse
    try:
        h = (urlparse(origem).hostname or "").lower()
    except ValueError:
        return False
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    return bool(host_req) and h == host_req.split(":")[0].lower()


@app.middleware("http")
async def _protege_csrf(request: Request, call_next):
    """Bloqueia requisições que mudam estado (POST/PUT/DELETE/PATCH) vindas de
    origem cruzada — a API comanda TX de RF, SMS e HackRF sem autenticação."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origem = request.headers.get("origin") or request.headers.get("referer") or ""
        if origem and not _origem_permitida(origem, request.headers.get("host", "")):
            return JSONResponse({"erro": "origem não permitida (proteção CSRF)"}, status_code=403)
    return await call_next(request)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clientes.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clientes.discard(ws)


@app.get("/api/status")
async def api_status():
    det, conf = proc.presenca()
    var = proc.variancia_rssi()
    return {
        "ok":        True,
        "fonte":     "real" if proc.modo_real else "simulação",
        "rssi":      round(proc.buf_rssi[-1], 1) if proc.buf_rssi else None,
        "variancia": round(var, 4),
        "threshold": THR_QUIETO,
        "presenca":  det,
        "confianca": conf,
        "atividade": proc.atividade(),
    }


@app.get("/")
async def raiz():
    return FileResponse(UI_PATH / "index.html")


# ─── Radio Multi-Mode: WebSocket de áudio em tempo real ─────────────────────
@app.websocket("/ws/radio")
async def ws_radio(ws: WebSocket,
                   freq: float = Query(98.1),
                   mode: str   = Query("WFM")):
    """
    Recebe freq em MHz + mode (WFM/NFM/AM), captura IQ do HackRF,
    demodula e envia PCM int16 mono em chunks de 100ms.
    mode: 'WFM' (FM broadcast), 'NFM' (VHF marino/UHF), 'AM' (aviação)
    Apenas um stream por vez (lock).
    """
    await ws.accept()

    if not _radio_lock.acquire(blocking=False):
        await ws.send_text(json.dumps({"erro": "rádio ocupado — outro stream ativo"}))
        await ws.close()
        return

    # Zera todos os outros usos do HackRF e pausa sensores
    hackrf_resource.zerar()
    sensor_hackrf.pausar()
    sensor_espectro.pausar()
    sensor_intel.pausar()
    if not hackrf_resource.acquire('radio', timeout=5.0):
        hackrf_resource.zerar()
        hackrf_resource.acquire('radio', timeout=3.0)

    _radio_ativo.set()
    loop   = asyncio.get_event_loop()
    proc   = None

    try:
        freq_hz = int(freq * 1_000_000)
        proc = subprocess.Popen(
            ["hackrf_transfer", "-r", "-",
             "-f", str(freq_hz),
             "-s", "2000000",
             "-g", "40",    # VGA
             "-l", "24",    # LNA
             "-a", "1"],    # amp
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

        # Avisa o cliente que o stream começou
        # Taxa de áudio por modo
        _mode = mode.upper()
        if _mode not in ("WFM", "NFM", "AM"):
            _mode = "WFM"
        _ar_audio = 48_000 if _mode == "WFM" else 8_000

        await ws.send_text(json.dumps({
            "ok": True,
            "freq_mhz": freq,
            "mode": _mode,
            "sample_rate": _ar_audio,
            "channels": 1,
            "bits": 16,
        }))

        import queue as _queue
        CHUNK_RD = 131_072   # leitura crua do pipe (drena rápido)
        TARGET   = 400_000   # ~100ms @ 2Msps para demodular de uma vez
        _buf_q: "_queue.Queue" = _queue.Queue(maxsize=64)

        def _reader():
            # Thread dedicada: só lê o stdout do hackrf_transfer e enfileira.
            # Mantém o pipe sempre drenado para o hackrf não travar (backpressure).
            while True:
                try:
                    d = proc.stdout.read(CHUNK_RD)
                except Exception:
                    d = b""
                if not d:
                    try: _buf_q.put_nowait(None)
                    except _queue.Full: pass
                    break
                try:
                    _buf_q.put_nowait(d)
                except _queue.Full:
                    # consumidor lento — descarta o mais antigo p/ não travar o hackrf
                    try: _buf_q.get_nowait()
                    except _queue.Empty: pass
                    try: _buf_q.put_nowait(d)
                    except _queue.Full: pass

        reader_t = threading.Thread(target=_reader, daemon=True, name="radio-reader")
        reader_t.start()

        async def _ler_e_enviar():
            acc = bytearray()
            while True:
                chunk = await loop.run_in_executor(None, _buf_q.get)
                if chunk is None:
                    break
                acc += chunk
                if len(acc) < TARGET:
                    continue
                data = bytes(acc)
                acc = bytearray()
                # Demodulação multi-mode em thread separada (CPU)
                pcm = await loop.run_in_executor(
                    None, _demodular, data, _mode, 2_000_000
                )
                try:
                    await ws.send_bytes(pcm.tobytes())
                except Exception:
                    break

        envio_task = asyncio.create_task(_ler_e_enviar())

        # Aguarda cliente desconectar ou enviar "STOP"
        try:
            while True:
                msg = await ws.receive_text()
                if msg.strip().upper() == "STOP":
                    break
        except (WebSocketDisconnect, Exception):
            pass

        envio_task.cancel()

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        _radio_ativo.clear()
        _radio_lock.release()
        hackrf_resource.release()
        sensor_hackrf.retomar()
        sensor_espectro.retomar()
        sensor_intel.retomar()


# ─── Sonda Near-Field (localizador físico por RSSI) ───────────────────────────
@app.websocket("/ws/nearfield")
async def ws_nearfield(ws: WebSocket,
                       freq: float = Query(433.9),
                       lna: int = Query(24),
                       vga: int = Query(20)):
    """Mede RSSI ao vivo numa frequência (ganhos FIXOS, sem AGC, p/ refletir distância).
    Envia {rssi_dbfs, ts} ~25 Hz. O cliente faz o tom/medidor 'hot-cold'."""
    await ws.accept()
    if not _radio_lock.acquire(blocking=False):
        await ws.send_text(json.dumps({"erro": "HackRF ocupado — pare outra atividade"}))
        await ws.close(); return

    hackrf_resource.zerar()
    sensor_hackrf.pausar(); sensor_espectro.pausar(); sensor_intel.pausar()
    if not hackrf_resource.acquire('nearfield', timeout=5.0):
        hackrf_resource.zerar(); hackrf_resource.acquire('nearfield', timeout=3.0)
    _radio_ativo.set()
    loop = asyncio.get_event_loop()
    proc = None
    try:
        import queue as _queue
        SR = 2_000_000
        proc = subprocess.Popen(
            ["hackrf_transfer", "-r", "-", "-f", str(int(freq * 1e6)),
             "-s", str(SR), "-g", str(int(vga)), "-l", str(int(lna)), "-a", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        await ws.send_text(json.dumps({"ok": True, "freq_mhz": freq, "lna": lna, "vga": vga}))

        CHUNK = 160_000          # ~40ms @ 2Msps (80k amostras) -> ~25 Hz
        q: "_queue.Queue" = _queue.Queue(maxsize=8)

        def _reader():
            while True:
                try:
                    d = proc.stdout.read(CHUNK)
                except Exception:
                    d = b""
                if not d:
                    try: q.put_nowait(None)
                    except _queue.Full: pass
                    break
                try: q.put_nowait(d)
                except _queue.Full:
                    try: q.get_nowait()
                    except _queue.Empty: pass
                    try: q.put_nowait(d)
                    except _queue.Full: pass
        threading.Thread(target=_reader, daemon=True, name="nf-reader").start()

        async def _enviar():
            while True:
                d = await loop.run_in_executor(None, q.get)
                if d is None:
                    break
                try:
                    a = np.frombuffer(d, dtype=np.int8).astype(np.float32)
                    a = a[:(a.size // 2) * 2]              # garante par (I,Q)
                    if a.size < 4:
                        continue
                    i = a[0::2]; qd = a[1::2]
                    pot = float(np.mean(i * i + qd * qd))
                    rssi = 10.0 * np.log10(pot / (127.0**2) + 1e-9)   # dBFS
                    await ws.send_text(json.dumps({"rssi": round(rssi, 1)}))
                except Exception:
                    break
        envio = asyncio.create_task(_enviar())
        try:
            while True:
                msg = await ws.receive_text()
                if msg.strip().upper() == "STOP":
                    break
        except (WebSocketDisconnect, Exception):
            pass
        envio.cancel()
    finally:
        if proc:
            proc.terminate()
            try: proc.wait(timeout=2)
            except Exception: pass
        _radio_ativo.clear()
        _radio_lock.release()
        hackrf_resource.release()
        sensor_hackrf.retomar(); sensor_espectro.retomar(); sensor_intel.retomar()


# ─── Localizador BLE (encontrar pessoas pelos aparelhos que carregam) ─────────
@app.websocket("/ws/ble")
async def ws_ble(ws: WebSocket):
    """Lista dispositivos BLE ao vivo (MAC+RSSI) p/ localização por gradiente.
    Usa o Bluetooth do PC (NÃO o HackRF). Mensagens do cliente:
      {"alvo": "AA:BB:.."}  -> seleciona aparelho a localizar
      {"alvo": null}        -> limpa o alvo
    Empurra estado() a ~5 Hz."""
    await ws.accept()
    if not localizador_ble.disponivel:
        await ws.send_text(json.dumps(
            {"erro": "Bluetooth/bleak indisponível: " + (localizador_ble.erro or "?")}))
        await ws.close(); return

    clientes_ble.add(ws)
    localizador_ble.iniciar()

    async def _receber():
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    j = json.loads(msg)
                    if "alvo" in j:
                        localizador_ble.selecionar(j["alvo"])
                except Exception:
                    pass
        except Exception:
            pass

    rt = asyncio.create_task(_receber())
    try:
        while True:
            await ws.send_text(json.dumps(localizador_ble.estado(), ensure_ascii=False))
            await asyncio.sleep(0.2)   # 5 Hz
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        rt.cancel()
        clientes_ble.discard(ws)
        if not clientes_ble:          # libera o rádio BT quando ninguém olha
            localizador_ble.parar()


@app.get("/api/ble")
async def api_ble():
    """Snapshot do localizador BLE (inicia o scan sob demanda)."""
    localizador_ble.iniciar()
    return localizador_ble.estado()


@app.post("/api/ble/alvo")
async def api_ble_alvo(payload: dict = Body(default={})):
    """Seleciona (ou limpa) o aparelho a localizar. Body: {"mac": "AA:.." | null}."""
    localizador_ble.selecionar(payload.get("mac"))
    return {"ok": True, "alvo": localizador_ble.alvo}


@app.post("/api/ble/stop")
async def api_ble_stop():
    """Para o scan BLE e libera o rádio Bluetooth."""
    localizador_ble.parar()
    return {"ok": True}


# ─── Monitor Ultrassônico (TSCM por áudio) ────────────────────────────────────
@app.websocket("/ws/ultrassom")
async def ws_ultrassom(ws: WebSocket):
    """Espectro ultrassônico ao vivo + tons suspeitos (beacons/exfil/jammer).
    Usa o microfone do PC (NÃO o HackRF). Empurra estado() a ~10 Hz."""
    await ws.accept()
    if not monitor_us.disponivel:
        await ws.send_text(json.dumps(
            {"erro": "áudio/sounddevice indisponível: " + (monitor_us.erro or "?")}))
        await ws.close(); return

    clientes_us.add(ws)
    monitor_us.iniciar()
    try:
        while True:
            await ws.send_text(json.dumps(monitor_us.estado(), ensure_ascii=False))
            await asyncio.sleep(0.1)   # 10 Hz
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        clientes_us.discard(ws)
        if not clientes_us:           # libera o microfone quando ninguém olha
            monitor_us.parar()


@app.get("/api/ultrassom")
async def api_ultrassom():
    """Snapshot do monitor ultrassônico (inicia a captura sob demanda)."""
    monitor_us.iniciar()
    return monitor_us.estado()


# ─── Inteligência Espectral ───────────────────────────────────────────────────
@app.websocket("/ws/intel")
async def ws_intel(ws: WebSocket):
    """WebSocket de inteligência espectral — atualiza a cada sweep."""
    await ws.accept()
    clientes_intel.add(ws)
    try:
        # Envia snapshot imediato ao conectar
        await ws.send_text(json.dumps(sensor_intel.inteligencia(), ensure_ascii=False))
        while True:
            await ws.receive_text()  # aguarda desconexão
    except (WebSocketDisconnect, Exception):
        clientes_intel.discard(ws)


@app.get("/api/intel")
async def api_intel():
    """Snapshot atual da inteligência espectral."""
    return sensor_intel.inteligencia()


@app.get("/api/radio/modo")
async def radio_modo_por_categoria(cat: str = Query(...)):
    """
    Retorna o modo de demodulação e se tem áudio para a categoria dada.
    cat: categoria do sinal (ex: 'FM', 'AERONAV', 'CELULAR', ...)
    """
    if cat in _CAT_NO_AUDIO:
        return {"audio": False, "modo": None,
                "motivo": f"{cat} usa protocolo digital/criptografado — sem áudio"}
    modo, ar = _CAT_MODO.get(cat, ("NFM", 8_000))
    return {"audio": True, "modo": modo, "ar": ar}


# ─── TSCM — Analista de Espectro (contra-vigilância) ──────────────────────────
@app.get("/api/tscm/bandas")
async def tscm_bandas():
    """Lista os presets de banda disponíveis para varredura."""
    return {k: {"label": v["label"]} for k, v in tscm_scanner.BANDAS.items()}


@app.post("/api/tscm/scan")
async def tscm_scan(body: dict):
    """
    Varre uma banda e devolve sinais classificados (escutas/câmeras).
    Body: { "banda": "audio"|"cam24"|"cam1258"|"gsm"|"full", "amp": bool }
    """
    banda = (body.get("banda") or "audio").lower()
    amp   = bool(body.get("amp", False))
    lna   = int(body.get("lna", 32))
    vga   = int(body.get("vga", 40))

    # garante o HackRF livre p/ o sweep (pausa sensores, não retoma — página é dona)
    sensor_hackrf.pausar()
    sensor_espectro.pausar()
    sensor_intel.pausar()

    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(
        None, tscm_scanner.escanear, banda, lna, vga, amp
    )
    return res


@app.post("/api/tscm/baseline")
async def tscm_baseline(body: dict):
    """Salva os sinais atuais como baseline da banda (p/ marcar NOVOS depois)."""
    banda  = (body.get("banda") or "audio").lower()
    sinais = body.get("sinais") or []
    n = tscm_scanner.salvar_baseline(banda, sinais)
    return {"ok": True, "banda": banda, "n": n}


@app.delete("/api/tscm/baseline")
async def tscm_baseline_limpar(banda: str = Query("audio")):
    tscm_scanner.limpar_baseline(banda.lower())
    return {"ok": True, "banda": banda}


@app.post("/api/tscm/video")
async def tscm_video_decode(body: dict):
    """
    Tenta decodificar vídeo analógico (câmera FM) na frequência dada.
    Body: { "freq": <MHz>, "padrao": "auto"|"NTSC"|"PAL", "sr": <Hz opc>, "amp": bool }
    Retorna um frame em tons de cinza (gray_b64 = bytes WxH base64) ou motivo da falha.
    """
    freq   = float(body.get("freq", 0))
    padrao = (body.get("padrao") or "auto")
    sr     = int(body.get("sr", 16_000_000))
    amp    = bool(body.get("amp", False))
    if freq <= 0:
        return {"ok": False, "motivo": "frequência inválida"}

    # garante HackRF livre (página é dona no modo tscm)
    sensor_hackrf.pausar(); sensor_espectro.pausar(); sensor_intel.pausar()

    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(
        None, lambda: tscm_video.analisar(freq, padrao, sr, 0.20, 24, 32, amp)
    )
    return res


# ─── Rede — Câmeras WiFi/IP (não usa HackRF) ──────────────────────────────────
@app.get("/api/rede/info")
async def rede_info():
    """Info da rede atual do PC (IP, SSID, sub-rede)."""
    return net_scanner.info_rede()


@app.post("/api/rede/scan")
async def rede_scan():
    """Escaneia a rede local e lista dispositivos, destacando câmeras IP/WiFi."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, net_scanner.escanear)


# ─── WiFi Red Team (testes autorizados) ───────────────────────────────────────
PORTAIS_PATH = Path(__file__).parent / "portais"


@app.get("/api/wifi/adaptadores")
async def wifi_adaptadores():
    """Lista as placas WiFi e capacidades."""
    loop = asyncio.get_event_loop()
    ad = await loop.run_in_executor(None, wifi_tools.listar_adaptadores)
    return {"adaptadores": ad, "acesso": wifi_tools.checar_acesso()}


@app.post("/api/wifi/scan")
async def wifi_scan(body: dict):
    """Escaneia APs vizinhos + detecção de rogue AP. Body: { interface?: str }"""
    interface = body.get("interface") or None
    loop = asyncio.get_event_loop()
    redes = await loop.run_in_executor(None, wifi_tools.escanear_redes, interface)
    alertas = wifi_tools.detectar_rogue(redes)
    acesso = wifi_tools.checar_acesso() if not redes else {"ok": True}
    return {"ok": True, "n": len(redes), "redes": redes, "rogue": alertas, "acesso": acesso}


@app.get("/api/wifi/portais")
async def wifi_portais():
    """Templates de portal disponíveis."""
    nomes = sorted(p.stem for p in PORTAIS_PATH.glob("*.html")) if PORTAIS_PATH.exists() else []
    return {"portais": nomes, "estado": wifi_tools.estado_portal()}


@app.post("/api/wifi/portal/arm")
async def wifi_portal_arm(body: dict):
    """Arma a campanha de portal (exige confirmação de autorização)."""
    if not body.get("autorizado"):
        return {"ok": False, "erro": "confirme a autorização para armar a campanha"}
    return {"ok": True, "estado": wifi_tools.armar_portal(body.get("campanha", ""), True)}


@app.post("/api/wifi/portal/desarmar")
async def wifi_portal_desarmar():
    return {"ok": True, "estado": wifi_tools.desarmar_portal()}


@app.post("/api/wifi/captura")
async def wifi_captura(body: dict, request: Request):
    """Recebe a submissão de um portal cativo (grava só se a campanha estiver armada)."""
    ip = request.client.host if request.client else ""
    return wifi_tools.registrar_captura(body.get("portal", "?"), body.get("campos", {}), ip)


@app.get("/api/wifi/capturas")
async def wifi_capturas():
    return {"estado": wifi_tools.estado_portal(), "capturas": wifi_tools.listar_capturas()}


@app.delete("/api/wifi/capturas")
async def wifi_capturas_limpar():
    wifi_tools.limpar_capturas()
    return {"ok": True}


@app.get("/portal/{nome}")
async def servir_portal(nome: str):
    """Serve uma página de portal cativo (standalone, sem nav)."""
    nome = re.sub(r"[^a-z0-9_-]", "", nome.lower())
    arq = PORTAIS_PATH / f"{nome}.html"
    if not arq.exists():
        raise HTTPException(404, "portal não encontrado")
    return FileResponse(arq)


# ─── Histórico e Relatórios ───────────────────────────────────────────────────
@app.post("/api/historico/salvar")
async def historico_salvar(body: dict):
    """Salva uma varredura no histórico. Body: { tipo, titulo, dados }"""
    return historico.salvar(body.get("tipo", "scan"), body.get("titulo", ""), body.get("dados", {}))


@app.get("/api/historico")
async def historico_listar():
    return {"entradas": historico.listar()}


@app.get("/api/historico/{eid}")
async def historico_obter(eid: str):
    d = historico.obter(eid)
    if not d:
        raise HTTPException(404, "entrada não encontrada")
    return d


@app.delete("/api/historico/{eid}")
async def historico_excluir(eid: str):
    return {"ok": historico.excluir(eid)}


@app.get("/relatorio/{eid}")
async def relatorio(eid: str):
    h = historico.relatorio_html(eid)
    if not h:
        raise HTTPException(404, "relatório não encontrado")
    return HTMLResponse(h)


# ─── Alertas e agendamento (monitor WiFi/rede em background) ──────────────────
@app.get("/api/alertas")
async def alertas_listar():
    return {"estado": agendador.estado(), "alertas": agendador.alertas()}


@app.post("/api/agendador/config")
async def agendador_config(body: dict):
    return agendador.configurar(
        ativo=body.get("ativo"),
        intervalo_s=body.get("intervalo_s"),
        tarefas=body.get("tarefas"),
    )


@app.delete("/api/alertas")
async def alertas_limpar():
    agendador.limpar()
    return {"ok": True}


# ─── Sentinela — inteligência de RF (baseline + monitoramento) ────────────────
@app.get("/api/sentinela/estado")
async def sentinela_estado():
    return {"estado": sentinela.estado(), "baselines": sentinela.listar()}


@app.post("/api/sentinela/aprender/iniciar")
async def sentinela_aprender_iniciar(body: dict):
    # garante o scanner rodando p/ aprender
    sensor_espectro.retomar(); sensor_intel.retomar()
    return sentinela.aprender_iniciar(body.get("local", "local"))


@app.post("/api/sentinela/aprender/salvar")
async def sentinela_aprender_salvar():
    return sentinela.aprender_salvar()


@app.post("/api/sentinela/comparar")
async def sentinela_comparar(body: dict):
    sensor_espectro.retomar(); sensor_intel.retomar()
    return sentinela.comparar(body.get("local", ""))


@app.post("/api/sentinela/monitorar")
async def sentinela_monitorar(body: dict):
    if body.get("on"):
        sensor_espectro.retomar(); sensor_intel.retomar()
    return sentinela.monitorar(body.get("local", ""), bool(body.get("on")))


@app.post("/api/sentinela/config")
async def sentinela_config(body: dict):
    return sentinela.config(margem_db=body.get("margem_db"))


@app.delete("/api/sentinela/baseline")
async def sentinela_excluir(local: str = Query(...)):
    return {"ok": sentinela.excluir(local)}


# ─── GPS — detector de jamming/interferência (defensivo, só recepção) ─────────
@app.post("/api/gps/medir")
async def gps_medir(body: dict = None):
    sensor_hackrf.pausar(); sensor_espectro.pausar(); sensor_intel.pausar()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, gps_monitor.medir)


@app.post("/api/gps/calibrar")
async def gps_calibrar():
    sensor_hackrf.pausar(); sensor_espectro.pausar(); sensor_intel.pausar()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, gps_monitor.calibrar)


# ─── Burst Hunter — caçador de transmissores intermitentes (RX) ───────────────
@app.post("/api/burst/cacar")
async def burst_cacar(body: dict):
    freq = float(body.get("freq", 0))
    sr   = int(body.get("sr", 8_000_000))
    dur  = float(body.get("dur", 2.0))
    amp  = bool(body.get("amp", False))
    if not (FREQ_MIN_MHZ <= freq <= FREQ_MAX_MHZ):
        return {"ok": False, "motivo": "frequência fora da faixa do HackRF (1–6000 MHz)"}
    if not (SR_MIN <= sr <= SR_MAX):
        return {"ok": False, "motivo": "sample rate fora da faixa (1–20 MHz)"}
    if not (0 < dur <= DUR_MAX_S):
        return {"ok": False, "motivo": f"duração fora da faixa (0–{DUR_MAX_S:g}s)"}
    sensor_hackrf.pausar(); sensor_espectro.pausar(); sensor_intel.pausar()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: burst_hunter.cacar(freq, sr, dur, 32, 40, amp))


# ─── ADS-B — radar de aviões 1090 MHz (RX) ────────────────────────────────────
@app.post("/api/adsb/start")
async def adsb_start():
    _parar_tudo_hackrf()          # libera o HackRF p/ o ADS-B
    sensor_adsb.iniciar()
    return {"ok": sensor_adsb.rodando, "pyModeS": adsb_scanner.PMS_OK}


@app.post("/api/adsb/stop")
async def adsb_stop():
    sensor_adsb.parar()
    return {"ok": True}


@app.get("/api/adsb")
async def adsb_estado():
    return sensor_adsb.estado()


# ─── ISM 433/868 — decoder OOK/ASK estilo rtl_433 (RX) ────────────────────────
@app.post("/api/ism/scan")
async def ism_scan(body: dict):
    freq = float(body.get("freq", 433.92))
    dur  = float(body.get("dur", 3.0))
    amp  = bool(body.get("amp", True))
    if not (FREQ_MIN_MHZ <= freq <= FREQ_MAX_MHZ):
        return {"ok": False, "motivo": "frequência fora da faixa do HackRF (1–6000 MHz)"}
    if not (0 < dur <= DUR_MAX_S):
        return {"ok": False, "motivo": f"duração fora da faixa (0–{DUR_MAX_S:g}s)"}
    sensor_hackrf.pausar(); sensor_espectro.pausar(); sensor_intel.pausar()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: ism_decoder.escanear(freq, dur, 32, 40, amp))


# ─── Rádio Operacional — Endpoints de HackRF ──────────────────────────────────
SINAIS_DIR = Path(__file__).parent / "sinais"
SINAIS_DIR.mkdir(exist_ok=True)

_radio_modo  = "idle"
_tx_proc: subprocess.Popen | None = None


@app.get("/api/hackrf/status")
async def hackrf_status():
    """Estado atual do HackRF e modo de operação."""
    return {
        "conectado": sensor_hackrf.conectado,
        "ocupado":   _radio_lock.locked(),
        "modo":      _radio_modo,
    }


@app.post("/api/hackrf/clonar")
async def clonar_sinal(
    freq:    float = Query(98.1, description="Frequência em MHz"),
    duracao: float = Query(2.0, ge=0.5, le=30.0, description="Duração em segundos"),
):
    """Captura IQ bruto de uma frequência e salva em arquivo .bin."""
    global _radio_modo
    if _radio_lock.locked():
        raise HTTPException(503, "HackRF ocupado — aguarde a operação atual terminar")
    if not sensor_hackrf.conectado:
        raise HTTPException(503, "HackRF não conectado")

    loop = asyncio.get_running_loop()

    def _capturar():
        global _radio_modo
        freq_hz = int(freq * 1_000_000)
        n_amos  = int(duracao * 2_000_000)
        nome    = f"{freq:.3f}MHz_{int(time.time())}.bin"
        caminho = SINAIS_DIR / nome
        _radio_modo = "clonando"
        with _radio_lock:
            r = subprocess.run(
                ["hackrf_transfer", "-r", str(caminho),
                 "-f", str(freq_hz), "-s", "2000000",
                 "-n", str(n_amos), "-g", "40", "-l", "24", "-a", "1"],
                capture_output=True, timeout=duracao + 15,
            )
        _radio_modo = "idle"
        if r.returncode != 0:
            if caminho.exists():
                caminho.unlink()
            raise RuntimeError(r.stderr.decode()[:300])
        return {
            "ok":      True,
            "arquivo": nome,
            "tamanho": caminho.stat().st_size,
            "freq":    freq,
            "duracao": duracao,
        }

    try:
        return await loop.run_in_executor(None, _capturar)
    except Exception as e:
        _radio_modo = "idle"
        raise HTTPException(500, str(e))


@app.get("/api/hackrf/sinais")
async def listar_sinais():
    """Lista os arquivos IQ capturados."""
    arqs = []
    for p in sorted(SINAIS_DIR.glob("*.bin"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        arqs.append({
            "nome":    p.name,
            "tamanho": p.stat().st_size,
            "criado":  int(p.stat().st_mtime),
        })
    return {"sinais": arqs}


@app.delete("/api/hackrf/sinais/{nome}")
async def deletar_sinal(nome: str):
    """Remove um arquivo de sinal capturado."""
    if "/" in nome or ".." in nome:
        raise HTTPException(400, "Nome inválido")
    p = SINAIS_DIR / nome
    if not p.exists():
        raise HTTPException(404, "Não encontrado")
    p.unlink()
    return {"ok": True}


@app.post("/api/hackrf/transmitir/iniciar")
async def tx_iniciar(
    freq:     float = Query(98.1, ge=1, le=6000, description="Frequência em MHz (faixa do HackRF)"),
    arquivo:  str   = Query(..., description="Nome do arquivo .bin em sinais/"),
    potencia: int   = Query(40, ge=0, le=47, description="TX VGA gain 0-47 dB"),
):
    """Inicia transmissão de um arquivo IQ salvo. ⚠ Requer licença de radioamador."""
    global _tx_proc, _radio_modo
    if "/" in arquivo or ".." in arquivo:
        raise HTTPException(400, "Nome inválido")
    p = SINAIS_DIR / arquivo
    if not p.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    if _radio_lock.locked():
        raise HTTPException(503, "HackRF ocupado")
    if not sensor_hackrf.conectado:
        raise HTTPException(503, "HackRF não conectado")

    _radio_lock.acquire()
    _radio_modo = "transmitindo"
    try:
        _tx_proc = subprocess.Popen(
            ["hackrf_transfer", "-t", str(p),
             "-f", str(int(freq * 1_000_000)),
             "-s", "2000000",
             "-x", str(potencia),
             "-a", "1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "pid": _tx_proc.pid, "freq": freq, "potencia": potencia}
    except Exception as e:
        _radio_lock.release()
        _radio_modo = "idle"
        raise HTTPException(500, str(e))


@app.post("/api/hackrf/transmitir/parar")
async def tx_parar():
    """Para a transmissão em andamento."""
    global _tx_proc, _radio_modo
    if _tx_proc:
        _tx_proc.terminate()
        try:
            _tx_proc.wait(timeout=3)
        except Exception:
            pass
        _tx_proc = None
    if _radio_lock.locked():
        try:
            _radio_lock.release()
        except RuntimeError:
            pass
    _radio_modo = "idle"
    return {"ok": True}


@app.get("/api/hackrf/rastrear")
async def rastrear_freqs(freqs: str = Query("", description="Lista de MHz separados por vírgula")):
    """
    Retorna potência (dBm) das frequências solicitadas via scanner de espectro.
    Consulta o ScannerEspectro já em execução — sem custo de novo processo HackRF.
    """
    if not freqs.strip():
        return {"leituras": [], "n_pontos": 0}

    vals: list[float] = []
    for s in freqs.split(","):
        try:
            vals.append(float(s.strip()))
        except ValueError:
            pass

    esp      = sensor_espectro.estado()
    f_arr    = esp.get("freqs", [])
    db_arr   = esp.get("dbs",   [])
    baseline = esp.get("baseline_ok", False)

    leituras = []
    for mhz in vals:
        hz = mhz * 1e6
        if not f_arr:
            leituras.append({"freq": mhz, "dbm": None})
            continue
        diffs = [abs(fx - hz) for fx in f_arr]
        idx   = diffs.index(min(diffs))
        if diffs[idx] < 3e6:
            leituras.append({"freq": mhz, "dbm": round(db_arr[idx], 1)})
        else:
            leituras.append({"freq": mhz, "dbm": None})

    return {
        "leituras":    leituras,
        "n_pontos":    len(f_arr),
        "baseline_ok": baseline,
        "ts":          time.time(),
    }


# ─── Auto-Scan: detecta picos no espectro ────────────────────────────────────
def _identificar_sinal(freq_mhz: float) -> str:
    """Retorna nome provável do sinal pela frequência."""
    if 88 <= freq_mhz <= 108:
        for f, nome in [
            (89.1,"CBN"),(89.5,"Jovem Pan"),(90.1,"Antena 1"),
            (91.3,"Mix FM"),(92.1,"Nova Brasil"),(93.7,"Transamérica"),
            (94.7,"Globo FM"),(95.1,"Metropolitana"),(96.5,"Band FM"),
            (97.5,"Jovem Pan 2"),(98.1,"Cultura FM"),(99.3,"Eldorado"),
            (100.1,"Boa Vontade"),(101.7,"Gospel"),(102.1,"Vanguarda"),
            (103.3,"Terra"),(104.7,"Pop Rock"),(105.1,"Rede Aleluia"),
            (106.3,"Rádio 9"),(107.1,"Tropical"),
        ]:
            if abs(freq_mhz - f) < 0.15:
                return nome
        return "FM Radio"
    if 108 <= freq_mhz <= 137:
        return "VHF Aviação"
    if 137 <= freq_mhz <= 174:
        return "VHF Móvel / PMR"
    if 162 <= freq_mhz <= 174:
        return "Rádio Náutico"
    if 300 <= freq_mhz <= 320:
        return "ISM 315 MHz (Controles)"
    if 430 <= freq_mhz <= 440:
        return "ISM 433 MHz (Controles/Portões)"
    if 446 <= freq_mhz <= 447:
        return "PMR 446 (Walkie-talkie)"
    if 450 <= freq_mhz <= 470:
        return "UHF Profissional"
    if 470 <= freq_mhz <= 700:
        return "TV UHF"
    if 700 <= freq_mhz <= 800:
        return "LTE 700 MHz"
    if 800 <= freq_mhz <= 900:
        return "UMTS 850 / LTE 850"
    if 856 <= freq_mhz <= 870:
        return "ISM 868 MHz (IoT/LoRa)"
    if 900 <= freq_mhz <= 928:
        return "ISM 915 MHz (LoRa/Sigfox)"
    return f"{freq_mhz:.1f} MHz"


@app.get("/api/hackrf/scan_auto")
async def scan_auto(
    min_mhz:     float = Query(88.0,  description="Frequência mínima em MHz"),
    max_mhz:     float = Query(900.0, description="Frequência máxima em MHz"),
    threshold:   float = Query(-75.0, description="Threshold mínimo em dBm"),
    min_sep_mhz: float = Query(1.0,   description="Separação mínima entre picos em MHz"),
):
    """
    Retorna picos de sinal detectados no espectro atual.
    Usa dados do ScannerEspectro em background (sem bloquear o HackRF).
    Inclui também sinais anômalos (spikes acima do baseline).
    """
    esp   = sensor_espectro.estado()
    freqs = esp.get("freqs", [])
    dbs   = esp.get("dbs",   [])

    if not freqs:
        return {
            "picos": [], "total": 0, "baseline_ok": False,
            "msg": "Scanner aguardando primeira varredura (~5s após iniciar)"
        }

    # Filtra pela faixa e threshold
    dados = [
        (float(f), float(d))
        for f, d in zip(freqs, dbs)
        if min_mhz <= float(f) <= max_mhz and float(d) >= threshold
    ]
    dados.sort(key=lambda x: x[0])

    if not dados:
        return {
            "picos": [], "total": 0, "baseline_ok": esp.get("baseline_ok", False),
            "msg": f"Nenhum sinal ≥ {threshold} dBm entre {min_mhz}–{max_mhz} MHz"
        }

    # Supressão de não-máximos: pega o pico mais forte dentro de cada janela min_sep_mhz
    freqs_arr = np.array([f for f, _ in dados])
    dbs_arr   = np.array([d for _, d in dados])
    usados    = set()
    picos     = []

    for idx in sorted(range(len(dbs_arr)), key=lambda i: -dbs_arr[i]):
        if idx in usados:
            continue
        freq = freqs_arr[idx]
        dbm  = dbs_arr[idx]
        for j in range(len(freqs_arr)):
            if abs(freqs_arr[j] - freq) < min_sep_mhz:
                usados.add(j)
        picos.append({
            "freq_mhz": round(freq, 3),
            "dbm":      round(dbm, 1),
            "nome":     _identificar_sinal(freq),
            "tipo":     "sinal",
        })

    # Adiciona anomalias (sinais transitórios/fantasmas — controles remotos, spikes)
    for a in esp.get("anomalos", []):
        f = float(a["freq_mhz"])
        if min_mhz <= f <= max_mhz:
            existe = any(abs(p["freq_mhz"] - f) < min_sep_mhz for p in picos)
            if not existe:
                picos.append({
                    "freq_mhz": round(f, 3),
                    "dbm":      round(float(a["dbm"]), 1),
                    "nome":     _identificar_sinal(f) + f" ⚡+{a['delta_db']}dB",
                    "tipo":     "anomalia",
                })

    picos.sort(key=lambda x: -x["dbm"])

    return {
        "picos":       picos[:20],
        "total":       len(picos),
        "baseline_ok": esp.get("baseline_ok", False),
        "ts":          time.time(),
        "msg":         f"{len(picos)} sinal(is) detectado(s)",
    }


# ─── IMSI / TMSI Scanner ──────────────────────────────────────────────────────
@app.websocket("/ws/imsi")
async def ws_imsi(ws: WebSocket):
    """WebSocket de capturas IMSI/TMSI — atualiza a cada 2s."""
    await ws.accept()
    clientes_imsi.add(ws)
    try:
        await ws.send_text(json.dumps(sensor_imsi.estado(), ensure_ascii=False))
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        clientes_imsi.discard(ws)


@app.get("/api/imsi")
async def api_imsi():
    """Snapshot completo do scanner IMSI."""
    return sensor_imsi.estado()


@app.post("/api/imsi/scan")
async def imsi_scan():
    """Inicia varredura de torres GSM com hackrf_sweep."""
    if not sensor_imsi.hackrf_ok:
        raise HTTPException(503, "HackRF não conectado / hackrf_sweep não encontrado")
    sensor_imsi.scan_torres()
    return {"ok": True, "msg": "Scan iniciado — aguarde ~60s"}


@app.post("/api/imsi/start")
async def imsi_start(freq_mhz: float = Query(None, description="Frequência da torre em MHz (opcional)")):
    """Inicia captura IMSI via grgsm_livemon_headless."""
    if not sensor_imsi.grgsm_ok:
        raise HTTPException(503, "gr-gsm não instalado — use o instalador: duplo clique em '📱 Instalar gr-gsm.command'")
    if sensor_imsi.capturando:
        raise HTTPException(409, "Captura já em andamento — use /api/imsi/stop primeiro")
    sensor_imsi.iniciar_captura(freq_mhz)
    return {"ok": True, "freq_mhz": freq_mhz}


@app.post("/api/imsi/stop")
async def imsi_stop():
    """Para a captura IMSI em andamento."""
    sensor_imsi.parar_captura()
    # retomar é chamado dentro de _retomar_todos_sensores no loop de captura,
    # mas se o usuário parar manualmente garantimos aqui também
    sensor_hackrf.retomar()
    sensor_espectro.retomar()
    sensor_intel.retomar()
    return {"ok": True}


@app.post("/api/imsi/limpar")
async def imsi_limpar():
    """Limpa todas as capturas e estatísticas."""
    sensor_imsi.limpar()
    return {"ok": True}


# ─── LLM / IA Local ──────────────────────────────────────────────────────────

# ─── Emergência Climática — Broadcast Multi-Frequência ───────────────────────

@app.get("/api/emergencia/status")
async def emergencia_status():
    return {
        "ativo":      _emergencia["ativo"],
        "progresso":  _emergencia["progresso"],
        "freq_atual": _emergencia["freq_atual"],
        "freq_idx":   _emergencia["freq_idx"],
        "freq_total": _emergencia["freq_total"],
        "iq_pronto":  _emergencia["iq_arquivo"] is not None and os.path.exists(_emergencia["iq_arquivo"] or ""),
    }


@app.post("/api/emergencia/preparar")
async def emergencia_preparar(body: dict):
    """
    Prepara o arquivo IQ para broadcast de emergência via TTS.
    Body: { "texto": "...", "voz": "Luciana", "repeticoes": 2 }
    """
    texto = body.get("texto", "").strip()
    if not texto:
        raise HTTPException(400, "texto vazio")

    voz        = body.get("voz", "Luciana")
    repeticoes = max(1, min(10, int(body.get("repeticoes", 3))))

    # Gera WAV via TTS em executor (bloqueante)
    loop = asyncio.get_event_loop()
    try:
        wav_bytes = await loop.run_in_executor(None, _tts_para_wav, texto, voz)
    except Exception as e:
        raise HTTPException(500, f"TTS falhou: {e}")

    # Multiplica o áudio pelas repetições (com pausa de 1s entre elas)
    import io, wave as _wave
    with _wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    silencio = b'\x00\x00' * sr   # 1s de silêncio

    pcm_total = (raw + silencio) * repeticoes
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as wf2:
        wf2.setnchannels(1); wf2.setsampwidth(2); wf2.setframerate(sr)
        wf2.writeframes(pcm_total)
    wav_final = buf.getvalue()

    # Modula para IQ FM em executor
    try:
        iq_bytes = await loop.run_in_executor(None, _modular_fm_iq, wav_final)
    except Exception as e:
        raise HTTPException(500, f"Modulação FM falhou: {e}")

    # Salva IQ em arquivo temporário
    iq_path = str(Path(__file__).parent / "sinais" / "_emergencia.iq")
    with open(iq_path, "wb") as f:
        f.write(iq_bytes)

    _emergencia["iq_arquivo"] = iq_path
    duracao = len(iq_bytes) / (2 * 2_000_000)   # int8 I+Q @ 2Msps

    return {
        "ok":        True,
        "iq_bytes":  len(iq_bytes),
        "duracao_s": round(duracao, 1),
        "repeticoes": repeticoes,
        "voz":       voz,
    }


def _broadcast_thread(freqs_hz: list[int], ganho: int, iq_path: str):
    """Transmite IQ em cada frequência sequencialmente."""
    _emergencia["_parar"].clear()
    _emergencia["ativo"]      = True
    _emergencia["freq_total"] = len(freqs_hz)

    # Pausa sensores — emergência tem prioridade máxima
    hackrf_resource.zerar()
    sensor_hackrf.pausar()
    sensor_espectro.pausar()
    sensor_intel.pausar()

    for idx, freq_hz in enumerate(freqs_hz):
        if _emergencia["_parar"].is_set():
            break

        freq_mhz = freq_hz / 1e6
        _emergencia["freq_idx"]   = idx + 1
        _emergencia["freq_atual"] = freq_mhz
        _emergencia["progresso"]  = f"Transmitindo {freq_mhz} MHz ({idx+1}/{len(freqs_hz)})"
        print(f"  🚨  EMERGÊNCIA: {freq_mhz} MHz (ganho {ganho} dB)")

        if not hackrf_resource.acquire("emergencia", timeout=10.0):
            hackrf_resource.zerar()
            hackrf_resource.acquire("emergencia", timeout=5.0)

        try:
            proc = subprocess.Popen(
                ["hackrf_transfer",
                 "-t", iq_path,
                 "-f", str(freq_hz),
                 "-s", "2000000",
                 "-x", str(ganho),
                 "-a", "1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Aguarda transmissão terminar (arquivo acaba) ou sinal de parar
            while proc.poll() is None:
                if _emergencia["_parar"].is_set():
                    proc.terminate()
                    break
                time.sleep(0.2)
        except Exception as e:
            print(f"  🚨  Erro broadcast: {e}")
        finally:
            hackrf_resource.release()

        # Pausa de 1s entre frequências
        if not _emergencia["_parar"].is_set():
            time.sleep(1.0)

    _emergencia["ativo"]     = False
    _emergencia["progresso"] = "Broadcast concluído"
    _emergencia["freq_atual"] = None
    sensor_hackrf.retomar()
    sensor_espectro.retomar()
    sensor_intel.retomar()
    print("  🚨  EMERGÊNCIA: broadcast concluído")


@app.post("/api/emergencia/transmitir")
async def emergencia_transmitir(body: dict):
    """
    Inicia broadcast de emergência multi-frequência.
    Body: { "freqs_mhz": [93.7, 98.1, 100.7], "ganho": 47 }
    O IQ deve ter sido preparado via /api/emergencia/preparar antes.
    """
    if _emergencia["ativo"]:
        raise HTTPException(409, "Broadcast já em andamento")

    iq_path = _emergencia.get("iq_arquivo")
    if not iq_path or not os.path.exists(iq_path):
        raise HTTPException(400, "Prepare o IQ primeiro via /api/emergencia/preparar")

    freqs_mhz = body.get("freqs_mhz", [])
    if not freqs_mhz:
        raise HTTPException(400, "Liste ao menos uma frequência")
    if not all(isinstance(f, (int, float)) and FREQ_MIN_MHZ <= f <= FREQ_MAX_MHZ for f in freqs_mhz):
        raise HTTPException(400, "Frequência fora da faixa do HackRF (1–6000 MHz)")

    ganho     = max(20, min(47, int(body.get("ganho", 47))))
    freqs_hz  = [int(f * 1e6) for f in freqs_mhz]

    t = threading.Thread(
        target=_broadcast_thread,
        args=(freqs_hz, ganho, iq_path),
        daemon=True, name="emergencia-broadcast"
    )
    t.start()
    _emergencia["_thread"] = t

    return {"ok": True, "freqs": freqs_mhz, "ganho": ganho}


@app.post("/api/emergencia/parar")
async def emergencia_parar():
    """Para o broadcast de emergência em andamento."""
    _emergencia["_parar"].set()
    subprocess.run(["pkill", "-9", "-f", "hackrf_transfer"], capture_output=True)
    _emergencia["ativo"]     = False
    _emergencia["progresso"] = "Broadcast interrompido"
    sensor_hackrf.retomar(); sensor_espectro.retomar(); sensor_intel.retomar()
    return {"ok": True}


@app.get("/api/llm/status")
async def llm_status():
    """Estado do LLM local e modelos disponíveis."""
    if not _LLM_OK:
        return {"ok": False, "erro": "llm_client não disponível"}
    try:
        modelos = llm_client.detectar_modelos()
        return {
            "ok":            True,
            "ollama":        True,
            "embed_model":   modelos.get("embed"),
            "chat_model":    modelos.get("chat"),
            "todos_modelos": modelos.get("todos", []),
            "chat_pronto":   modelos.get("chat") is not None,
            "instrucao":     None if modelos.get("chat") else
                "ollama pull qwen2.5:3b   ← 1.9 GB, ótimo em português",
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@app.post("/api/llm/chat")
async def llm_chat(body: dict):
    """
    Chat com o LLM local com contexto RF em tempo real.
    Body: { "mensagem": "...", "historico": [...] }
    """
    if not _LLM_OK:
        raise HTTPException(503, "LLM não disponível")

    mensagem = body.get("mensagem", "").strip()
    if not mensagem:
        raise HTTPException(400, "mensagem vazia")

    historico = body.get("historico", [])

    # Monta contexto RF atual
    try:
        ctx = {
            "rssi":      None,
            "variancia": None,
            "espectro":  sensor_espectro.estado(),
            "sinais":    sensor_intel.inteligencia().get("sinais", []),
            "hackrf":    sensor_hackrf.estado(),
        }
    except Exception:
        ctx = {}

    loop = asyncio.get_event_loop()
    try:
        resposta = await loop.run_in_executor(
            None, llm_client.chat, mensagem, ctx, None, historico
        )
    except Exception as e:
        raise HTTPException(500, f"Erro LLM: {e}")

    return {"ok": True, "resposta": resposta}


@app.post("/api/llm/classificar")
async def llm_classificar(body: dict):
    """
    Classifica um sinal por embedding (nomic-embed-text).
    Body: { "freq_mhz": 433.92, "dbm": -68, "descricao": "..." }
    """
    if not _LLM_OK:
        raise HTTPException(503, "LLM não disponível")

    freq  = body.get("freq_mhz", 0)
    dbm   = body.get("dbm", -80)
    desc  = body.get("descricao", "")

    loop = asyncio.get_event_loop()
    resultado = await loop.run_in_executor(
        None, llm_client.classificar_sinal, freq, dbm, desc
    )
    return {"ok": True, **resultado}


# WebSocket streaming de chat (para respostas longas)
@app.websocket("/ws/llm")
async def ws_llm(ws: WebSocket):
    """WebSocket para chat com streaming de tokens do LLM."""
    await ws.accept()
    if not _LLM_OK:
        await ws.send_text(json.dumps({"erro": "LLM não disponível"}))
        await ws.close()
        return
    try:
        while True:
            raw = await ws.receive_text()
            body = json.loads(raw)
            mensagem = body.get("mensagem", "").strip()
            if not mensagem:
                continue

            historico = body.get("historico", [])
            ctx = {
                "espectro": sensor_espectro.estado(),
                "sinais":   sensor_intel.inteligencia().get("sinais", []),
                "hackrf":   sensor_hackrf.estado(),
            }

            await ws.send_text(json.dumps({"tipo": "inicio"}))
            loop = asyncio.get_event_loop()

            # Streaming de tokens via executor (llm_client usa generator síncrono)
            def _stream():
                chunks = []
                for chunk in llm_client.chat_stream(mensagem, ctx, None, historico):
                    chunks.append(chunk)
                return chunks

            chunks = await loop.run_in_executor(None, _stream)
            full = "".join(chunks)
            await ws.send_text(json.dumps({"tipo": "texto", "conteudo": full}))
            await ws.send_text(json.dumps({"tipo": "fim"}))

    except (WebSocketDisconnect, Exception):
        pass


# ─── Emergência: contatos e SMS ───────────────────────────────────────────────

_CONTATOS_PATH = Path(__file__).parent / "contatos_emergencia.json"

_sms_status: dict = {"enviados": 0, "falhas": 0, "pendentes": 0, "ativo": False, "log": []}


def _carregar_contatos() -> list:
    if _CONTATOS_PATH.exists():
        try:
            return json.loads(_CONTATOS_PATH.read_text())
        except Exception:
            return []
    return []


def _salvar_contatos(lista: list):
    _CONTATOS_PATH.write_text(json.dumps(lista, ensure_ascii=False, indent=2))


@app.get("/api/emergencia/contatos")
async def contatos_listar():
    return {"contatos": _carregar_contatos()}


@app.post("/api/emergencia/contatos")
async def contatos_salvar(body: dict):
    lista = body.get("contatos", [])
    _salvar_contatos(lista)
    return {"ok": True, "total": len(lista)}


async def _enviar_sms_twilio(para: str, mensagem: str) -> dict:
    import urllib.request, urllib.parse, base64
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "")
    if not all([account_sid, auth_token, from_number]):
        return {"ok": False, "erro": "Twilio não configurado — adicione TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e TWILIO_FROM_NUMBER ao .env"}
    url  = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({"To": para, "From": from_number, "Body": mensagem}).encode()
    cred = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req  = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {cred}",
        "Content-Type":  "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read())
            return {"ok": True, "sid": r.get("sid", "")}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


async def _disparar_sms_async(mensagem: str, contatos: list):
    _sms_status.update({"enviados": 0, "falhas": 0, "pendentes": len(contatos), "ativo": True, "log": []})
    for c in contatos:
        tel = c.get("telefone", "")
        if not tel:
            _sms_status["pendentes"] = max(0, _sms_status["pendentes"] - 1)
            continue
        r = await _enviar_sms_twilio(tel, mensagem)
        entry = {"nome": c.get("nome", tel), "telefone": tel, **r}
        _sms_status["log"].append(entry)
        if r["ok"]:
            _sms_status["enviados"] += 1
        else:
            _sms_status["falhas"] += 1
        _sms_status["pendentes"] = max(0, _sms_status["pendentes"] - 1)
    _sms_status["ativo"] = False


@app.post("/api/emergencia/sms")
async def emergencia_sms(body: dict):
    """Dispara SMS para contatos. Body: { mensagem, contatos? }"""
    mensagem = body.get("mensagem", "").strip()
    if not mensagem:
        raise HTTPException(400, "mensagem vazia")
    contatos = body.get("contatos") or _carregar_contatos()
    if not contatos:
        raise HTTPException(400, "nenhum contato cadastrado")
    asyncio.create_task(_disparar_sms_async(mensagem, contatos))
    return {"ok": True, "total": len(contatos)}


@app.get("/api/emergencia/sms/status")
async def emergencia_sms_status():
    return _sms_status


def _broadcast_multi_thread(center_hz: int, sr_iq: int, ganho: int, iq_path: str, n_canais: int):
    """Transmissão única cobrindo TODOS os canais FM simultaneamente."""
    _emergencia["_parar"].clear()
    _emergencia["ativo"]      = True
    _emergencia["freq_total"] = 1
    _emergencia["freq_idx"]   = 0
    _emergencia["progresso"]  = f"Gerando IQ composto ({n_canais} canais)..."
    _emergencia["freq_atual"] = center_hz / 1e6

    hackrf_resource.zerar()
    sensor_hackrf.pausar()
    sensor_espectro.pausar()
    sensor_intel.pausar()

    if not hackrf_resource.acquire("emergencia", timeout=15.0):
        hackrf_resource.zerar()
        hackrf_resource.acquire("emergencia", timeout=5.0)

    _emergencia["progresso"] = f"Transmitindo {n_canais} canais @ {center_hz/1e6:.0f} MHz centro"
    _emergencia["freq_idx"]  = 1

    try:
        proc = subprocess.Popen(
            ["hackrf_transfer",
             "-t", iq_path,
             "-f", str(center_hz),
             "-s", str(sr_iq),
             "-x", str(ganho),
             "-a", "1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        while proc.poll() is None:
            if _emergencia["_parar"].is_set():
                proc.terminate()
                break
            time.sleep(0.2)
    except Exception as e:
        print(f"  🚨  Erro broadcast multi: {e}")
    finally:
        hackrf_resource.release()

    _emergencia["ativo"]     = False
    _emergencia["progresso"] = "Broadcast concluído"
    _emergencia["freq_atual"] = None
    sensor_hackrf.retomar()
    sensor_espectro.retomar()
    sensor_intel.retomar()
    print(f"  🚨  EMERGÊNCIA: broadcast simultâneo concluído ({n_canais} canais)")


@app.post("/api/emergencia/disparar")
async def emergencia_disparar(body: dict):
    """
    Dispara FM broadcast SIMULTÂNEO em todos os canais + SMS em paralelo.
    Body: { "texto", "voz", "repeticoes", "freqs_mhz", "ganho", "contatos" }
    FM: composite IQ @ 20 Msps, center 98 MHz → cobre 88–108 MHz de uma vez.
    """
    if _emergencia["ativo"]:
        raise HTTPException(409, "Broadcast já em andamento — pare antes de novo disparo")

    texto = body.get("texto", "").strip()
    if not texto:
        raise HTTPException(400, "texto vazio")

    voz        = body.get("voz", "Luciana")
    repeticoes = max(1, min(6, int(body.get("repeticoes", 2))))
    ganho      = max(20, min(47, int(body.get("ganho", 47))))
    freqs_mhz  = body.get("freqs_mhz") or [round(88.0 + i * 0.2, 1) for i in range(101)]
    if not all(isinstance(f, (int, float)) and FREQ_MIN_MHZ <= f <= FREQ_MAX_MHZ for f in freqs_mhz):
        raise HTTPException(400, "Frequência fora da faixa do HackRF (1–6000 MHz)")
    center_mhz = 98.0
    sr_iq      = 20_000_000

    loop = asyncio.get_event_loop()

    # 1. TTS → WAV com repetições
    _emergencia["progresso"] = "Gerando áudio TTS..."
    try:
        import io as _io, wave as _wave
        wav_bytes = await loop.run_in_executor(None, _tts_para_wav, texto, voz)
        with _wave.open(_io.BytesIO(wav_bytes), "rb") as wf:
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        silencio  = b"\x00\x00" * sr                  # 1 s pausa
        pcm_total = (raw + silencio) * repeticoes
        buf = _io.BytesIO()
        with _wave.open(buf, "wb") as wf2:
            wf2.setnchannels(1); wf2.setsampwidth(2); wf2.setframerate(sr)
            wf2.writeframes(pcm_total)
        wav_final = buf.getvalue()
    except Exception as e:
        raise HTTPException(500, f"TTS falhou: {e}")

    # 2. WAV → IQ FM COMPOSTO (todos os canais simultâneos @ 20 Msps)
    _emergencia["progresso"] = f"Modulando FM composto ({len(freqs_mhz)} canais)..."
    try:
        iq_bytes = await loop.run_in_executor(
            None, _modular_fm_multi, wav_final, freqs_mhz, center_mhz, sr_iq
        )
    except Exception as e:
        raise HTTPException(500, f"Modulação FM falhou: {e}")

    sinais_path = Path(__file__).parent / "sinais"
    sinais_path.mkdir(exist_ok=True)
    iq_path = str(sinais_path / "_emergencia_multi.iq")
    with open(iq_path, "wb") as f:
        f.write(iq_bytes)
    _emergencia["iq_arquivo"] = iq_path

    # 3. Thread de transmissão única (20 Msps, center 98 MHz)
    t = threading.Thread(
        target=_broadcast_multi_thread,
        args=(int(center_mhz * 1e6), sr_iq, ganho, iq_path, len(freqs_mhz)),
        daemon=True, name="emergencia-broadcast"
    )
    t.start()
    _emergencia["_thread"] = t

    # 4. SMS em paralelo (não bloqueia o FM)
    contatos = body.get("contatos") or _carregar_contatos()
    if contatos:
        asyncio.create_task(_disparar_sms_async(texto, contatos))

    duracao_s = len(iq_bytes) / (2 * sr_iq)
    return {
        "ok":            True,
        "modo":          "simultaneo",
        "fm_canais":     len(freqs_mhz),
        "center_mhz":    center_mhz,
        "sr_msps":       sr_iq / 1e6,
        "sms_contatos":  len(contatos),
        "duracao_audio": round(duracao_s, 1),
    }


# ─── Controle universal do HackRF (botão START/STOP de cada página) ───────────

def _parar_tudo_hackrf():
    """Para TODA atividade no HackRF e pausa todos os sensores."""
    if _emergencia["ativo"]:
        _emergencia["_parar"].set()
    hackrf_resource.zerar()
    try: sensor_imsi.parar_captura()
    except Exception: pass
    try: sensor_adsb.parar()
    except Exception: pass
    sensor_hackrf.pausar()
    sensor_espectro.pausar()
    sensor_intel.pausar()
    hackrf_resource.matar("hackrf_transfer", "hackrf_sweep")


@app.post("/api/hackrf/start")
async def hackrf_start(body: dict):
    """
    Para tudo no HackRF e inicia o processo que a página pede.
    Body: { "modo": "completo"|"scanner"|"doppler"|"imsi"|"radio"|"emergencia" }
    """
    modo = (body.get("modo") or "completo").lower()

    # 1. Libera o HackRF de qualquer atividade anterior
    _parar_tudo_hackrf()

    # 2. Inicia o que esta página precisa
    iniciados = []
    if modo in ("completo", "dashboard", "3d", "saude", "health"):
        sensor_hackrf.retomar(); sensor_espectro.retomar(); sensor_intel.retomar()
        iniciados = ["wifi+doppler", "espectro", "intel"]
    elif modo == "doppler":
        sensor_hackrf.retomar()
        iniciados = ["wifi+doppler"]
    elif modo in ("scanner", "intel", "espectro"):
        sensor_espectro.retomar(); sensor_intel.retomar()
        iniciados = ["espectro", "intel"]
    elif modo in ("imsi", "intercept"):
        sensor_imsi.iniciar_captura()
        iniciados = ["imsi/grgsm"]
    elif modo in ("radio", "emergencia", "idle", "tscm", "analista", "gps"):
        # HackRF fica livre — a própria página assume (varrer / sintonizar / medir)
        iniciados = []

    return {"ok": True, "modo": modo, "iniciados": iniciados, "dono": hackrf_resource.dono()}


@app.post("/api/hackrf/stop")
async def hackrf_stop():
    """Para tudo no HackRF e devolve o dispositivo (todos os sensores pausados)."""
    _parar_tudo_hackrf()
    return {"ok": True, "livre": hackrf_resource.livre()}


app.mount("/", StaticFiles(directory=str(UI_PATH), html=True), name="ui")

if __name__ == "__main__":
    print()
    print("  📡  mtzRF — Plataforma RF + Áudio + HackRF")
    print(f"  🌐  http://localhost:{PORTA}")
    print(f"  ℹ️   HackRF: scan 2.4GHz + doppler + espectro 88-900MHz")
    print()
    host = os.getenv("MTZRF_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ⚠️   bind em {host} (fora de localhost) — a API NÃO tem autenticação;")
        print(f"       exponha só em rede confiável, atrás de VPN/firewall (env MTZRF_HOST).")
    uvicorn.run(app, host=host, port=PORTA, log_level="warning")
