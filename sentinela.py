#!/usr/bin/env python3
"""
mtzRF — Sentinela de RF (inteligência: varredura + monitoramento)
==================================================================
Transforma o scanner de inteligência num INSTRUMENTO de verdade:

  • Aprender baseline de um LOCAL  — o "RF normal" daquele lugar, salvo em disco
  • Comparar (varredura pontual)   — o que mudou vs. o baseline (sinal novo/mais forte)
  • Monitorar 24/7 (sentinela)     — deixa ligado; dispara ALERTA quando surge anomalia

Reaproveita o ScannerInteligente (sweep 88 MHz–6 GHz contínuo) e os sistemas de
alertas/histórico/SMS já existentes. Só recepção — nunca transmite.
"""

import os
import re
import time
import json
import threading

import numpy as np

from intelligence_scanner import _classificar

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baselines")
os.makedirs(_DIR, exist_ok=True)

ABS_DBM = -75.0          # ignora bins abaixo disso (ruído)

_intel = None            # instância do ScannerInteligente
_alerta_fn = None        # callable(tipo, nivel, msg)
_hist_fn = None          # callable(tipo, titulo, dados)

_cfg = {"local": None, "aprendendo": False, "monitorando": False,
        "margem_db": 8.0}
_estado = {"n_aprend": 0, "ult_sweep": -1, "ultima_cmp": None, "ultimo_n_anom": 0}
_acc = {"freqs": None, "vmax": None, "n": 0}      # acumulador de aprendizado
_cand: dict = {}          # candidatos a anomalia: chave -> nº de comparações consecutivas
_lock = threading.Lock()


# ── util ──────────────────────────────────────────────────────────────────────────
def _safe(nome: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", (nome or "local").strip())[:48] or "local"


def _path(nome: str) -> str:
    return os.path.join(_DIR, _safe(nome) + ".npz")


def configurar(intel, alerta_fn=None, hist_fn=None):
    global _intel, _alerta_fn, _hist_fn
    _intel = intel
    _alerta_fn = alerta_fn
    _hist_fn = hist_fn
    threading.Thread(target=_loop, daemon=True, name="sentinela").start()


# ── baselines em disco ──────────────────────────────────────────────────────────────
def listar() -> list:
    out = []
    for f in sorted(os.listdir(_DIR)):
        if not f.endswith(".npz"):
            continue
        try:
            d = np.load(os.path.join(_DIR, f), allow_pickle=False)
            out.append({
                "local": str(d["local"]),
                "ts": str(d["ts"]),
                "n_sweeps": int(d["n_sweeps"]),
                "n_bins": int(len(d["freqs_hz"])),
                "f_min_mhz": round(float(d["freqs_hz"][0]) / 1e6, 1),
                "f_max_mhz": round(float(d["freqs_hz"][-1]) / 1e6, 1),
            })
        except (OSError, KeyError, ValueError):
            pass
    return out


def _carregar(nome: str):
    p = _path(nome)
    if not os.path.exists(p):
        return None
    try:
        d = np.load(p, allow_pickle=False)
        return {"freqs": d["freqs_hz"], "base": d["base_dbm"],
                "local": str(d["local"]), "ts": str(d["ts"]), "n_sweeps": int(d["n_sweeps"])}
    except (OSError, KeyError, ValueError):
        return None


def excluir(nome: str) -> bool:
    p = _path(nome)
    if os.path.exists(p):
        try:
            os.remove(p)
            return True
        except OSError:
            return False
    return False


# ── aprendizado ───────────────────────────────────────────────────────────────────
def aprender_iniciar(local: str) -> dict:
    with _lock:
        _acc["freqs"] = None
        _acc["vmax"] = None
        _acc["n"] = 0
        _cfg["local"] = local
        _cfg["aprendendo"] = True
        _cfg["monitorando"] = False
        _estado["n_aprend"] = 0
        _estado["ult_sweep"] = _intel.sweeps if _intel else -1
    return estado()


def _acumular(esp: dict):
    """Acumula o MÁXIMO por bin (captura tudo que normalmente está presente)."""
    f = esp["freqs_hz"]
    d = esp["dbm"]
    if _acc["freqs"] is None:
        _acc["freqs"] = f.copy()
        _acc["vmax"] = d.copy()
    else:
        if len(f) != len(_acc["freqs"]):
            d = np.interp(_acc["freqs"], f, d)
        _acc["vmax"] = np.maximum(_acc["vmax"], d)
    _acc["n"] += 1
    _estado["n_aprend"] = _acc["n"]


def aprender_salvar() -> dict:
    with _lock:
        _cfg["aprendendo"] = False
        if _acc["freqs"] is None or _acc["n"] == 0:
            return {"ok": False, "erro": "nenhuma varredura capturada (o scanner está rodando?)"}
        nome = _cfg["local"] or "local"
        np.savez(_path(nome),
                 local=nome,
                 ts=time.strftime("%Y-%m-%d %H:%M:%S"),
                 n_sweeps=_acc["n"],
                 freqs_hz=_acc["freqs"],
                 base_dbm=_acc["vmax"])
        return {"ok": True, "local": nome, "n_sweeps": _acc["n"], "n_bins": int(len(_acc["freqs"]))}


# ── comparação / anomalias ────────────────────────────────────────────────────────
def _detectar_anomalias(freqs_base, base, freqs_cur, cur, margem) -> list:
    """Anomalias = bins onde o atual supera o baseline + margem (e acima do ruído)."""
    if len(freqs_cur) != len(freqs_base):
        cur = np.interp(freqs_base, freqs_cur, cur)
    delta = cur - base
    mask = (delta >= margem) & (cur >= ABS_DBM)
    if not mask.any():
        return []
    # agrupa bins contíguos
    idx = np.where(mask)[0]
    grupos = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
    anom = []
    for g in grupos:
        pico = g[int(np.argmax(cur[g]))]
        fhz = float(freqs_base[pico])
        cat, cor, desc = _classificar(fhz)
        anom.append({
            "freq_mhz": round(fhz / 1e6, 3),
            "dbm": round(float(cur[pico]), 1),
            "base_dbm": round(float(base[pico]), 1),
            "delta_db": round(float(cur[pico] - base[pico]), 1),
            "bw_khz": round(float((freqs_base[g[-1]] - freqs_base[g[0]]) / 1e3 + 1000), 0),
            "cat": cat, "cor": cor, "desc": desc,
        })
    anom.sort(key=lambda a: -a["delta_db"])
    return anom


def comparar(nome: str) -> dict:
    b = _carregar(nome)
    if not b:
        return {"ok": False, "erro": "baseline não encontrado"}
    if _intel is None:
        return {"ok": False, "erro": "scanner indisponível"}
    esp = _intel.espectro_bruto()
    if esp is None:
        return {"ok": False, "erro": "scanner ainda não varreu — ative o scanner (modo completo) e aguarde"}
    anom = _detectar_anomalias(b["freqs"], b["base"], esp["freqs_hz"], esp["dbm"], _cfg["margem_db"])
    _estado["ultima_cmp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _estado["ultimo_n_anom"] = len(anom)
    # panorama leve p/ o gráfico
    n = len(b["freqs"])
    passo = max(1, n // 1000)
    cur = esp["dbm"]
    if len(cur) != n:
        cur = np.interp(b["freqs"], esp["freqs_hz"], cur)
    return {
        "ok": True, "local": nome, "baseline_ts": b["ts"], "n_anomalias": len(anom),
        "anomalias": anom,
        "panorama": {
            "freqs": [round(float(b["freqs"][i]) / 1e6, 2) for i in range(0, n, passo)],
            "atual": [round(float(cur[i]), 1) for i in range(0, n, passo)],
            "base":  [round(float(b["base"][i]), 1) for i in range(0, n, passo)],
        },
    }


def monitorar(nome: str, on: bool) -> dict:
    with _lock:
        if on:
            if not _carregar(nome):
                return {"ok": False, "erro": "baseline não encontrado"}
            _cfg["local"] = nome
            _cfg["monitorando"] = True
            _cfg["aprendendo"] = False
            _cand.clear()
            _estado["ult_sweep"] = _intel.sweeps if _intel else -1
        else:
            _cfg["monitorando"] = False
    return estado()


def config(margem_db=None) -> dict:
    if margem_db is not None:
        _cfg["margem_db"] = max(3.0, float(margem_db))
    return estado()


def estado() -> dict:
    return {
        "local": _cfg["local"], "aprendendo": _cfg["aprendendo"],
        "monitorando": _cfg["monitorando"], "margem_db": _cfg["margem_db"],
        "n_aprend": _estado["n_aprend"], "ultima_cmp": _estado["ultima_cmp"],
        "ultimo_n_anom": _estado["ultimo_n_anom"],
        "scanner_ok": bool(_intel and _intel.espectro_bruto() is not None),
    }


# ── loop de fundo ────────────────────────────────────────────────────────────────
def _loop():
    while True:
        try:
            if _intel is None:
                time.sleep(2); continue
            sw = _intel.sweeps
            if sw == _estado["ult_sweep"]:
                time.sleep(2); continue          # nada novo
            esp = _intel.espectro_bruto()
            if esp is None:
                time.sleep(2); continue
            _estado["ult_sweep"] = sw

            if _cfg["aprendendo"]:
                _acumular(esp)

            elif _cfg["monitorando"] and _cfg["local"]:
                b = _carregar(_cfg["local"])
                if b:
                    anom = _detectar_anomalias(b["freqs"], b["base"],
                                               esp["freqs_hz"], esp["dbm"], _cfg["margem_db"])
                    _estado["ultima_cmp"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    _estado["ultimo_n_anom"] = len(anom)
                    _avaliar_anomalias(anom)
        except Exception:
            pass
        time.sleep(2)


def _avaliar_anomalias(anom: list):
    """Alerta só anomalias persistentes (>= 2 varreduras), uma vez cada."""
    vistos = set()
    for a in anom:
        chave = f"{round(a['freq_mhz'], 1)}|{a['cat']}"
        vistos.add(chave)
        c = _cand.get(chave, {"n": 0, "alertado": False, "a": a})
        c["n"] += 1
        c["a"] = a
        if c["n"] == 2 and not c["alertado"]:
            c["alertado"] = True
            msg = (f"Sinal novo/anômalo: {a['freq_mhz']} MHz "
                   f"({a['cat']}) {a['dbm']} dBm · +{a['delta_db']} dB vs baseline "
                   f"[{_cfg['local']}]")
            nivel = 3 if a["delta_db"] >= 15 else 2
            if _alerta_fn:
                try: _alerta_fn("sentinela", nivel, msg)
                except Exception: pass
            if _hist_fn:
                try: _hist_fn("sentinela", f"Anomalia {a['freq_mhz']}MHz", {"anomalia": a, "local": _cfg["local"]})
                except Exception: pass
        _cand[chave] = c
    # reseta candidatos que sumiram
    for k in list(_cand.keys()):
        if k not in vistos:
            _cand.pop(k, None)
