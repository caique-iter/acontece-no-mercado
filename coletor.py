#!/usr/bin/env python3
"""
Coletor de Notícias — Acontece no Mercado
Busca notícias via RSS, aplica múltiplas tags por notícia e publica no GitHub Pages.
"""

import json
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Instalar dependências se necessário ───────────────────────────────────────
def pip_install(*pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs,
                           "-q", "--break-system-packages"])

try:
    import feedparser
except ImportError:
    pip_install("feedparser")
    import feedparser

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    pip_install("requests", "beautifulsoup4")
    import requests
    from bs4 import BeautifulSoup

# ── Caminhos ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "data" / "noticias.json"

# ── Carregar / salvar ─────────────────────────────────────────────────────────
def carregar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def carregar_dados():
    if DATA_PATH.exists():
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"noticias": [], "ultima_atualizacao": None, "total": 0}

def salvar_dados(dados):
    DATA_PATH.parent.mkdir(exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

# ── ID único por URL ──────────────────────────────────────────────────────────
def gerar_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]

# ── Verificar menção a concorrente ────────────────────────────────────────────
def detectar_concorrente(texto: str, concorrentes: dict) -> bool:
    t = texto.lower()
    todos = concorrentes.get("rj", []) + concorrentes.get("rs", [])
    return any(c.lower() in t for c in todos)

# ── Multi-tag: retorna lista de todas as categorias aplicáveis ────────────────
def taguear(titulo: str, resumo: str, categorias: list, concorrentes: dict) -> list[str]:
    texto = (titulo + " " + resumo).lower()
    tags = []

    # Detectar concorrentes primeiro (prioridade)
    if detectar_concorrente(texto, concorrentes):
        tags.append("Concorrentes")

    for cat in categorias:
        if cat["nome"] == "Concorrentes":
            continue  # já tratado acima
        for palavra in cat.get("palavras_chave", []):
            if palavra.lower() in texto:
                if cat["nome"] not in tags:
                    tags.append(cat["nome"])
                break  # uma palavra já basta para essa categoria

    # Categoria padrão se nenhuma bateu
    if not tags:
        tags = ["Novidades no Setor"]

    return tags

# ── Limpar HTML de resumo ─────────────────────────────────────────────────────
def limpar_html(texto: str, max_chars: int = 300) -> str:
    if "<" in texto:
        texto = BeautifulSoup(texto, "html.parser").get_text(separator=" ")
    return " ".join(texto.split())[:max_chars].strip()

# ── Coletar via RSS ───────────────────────────────────────────────────────────
def coletar_rss(fonte: dict, categorias: list, concorrentes: dict, max_n: int) -> list:
    noticias = []
    if not fonte.get("rss"):
        return noticias

    print(f"  → {fonte['nome']}")
    try:
        feed = feedparser.parse(fonte["rss"])
        for entry in feed.entries[:max_n]:
            titulo = entry.get("title", "").strip()
            url = entry.get("link", "").strip()
            if not titulo or not url:
                continue

            resumo_raw = entry.get("summary", entry.get("description", ""))
            resumo = limpar_html(resumo_raw)

            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                data = datetime(*pub[:6], tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                data = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            tags = taguear(titulo, resumo, categorias, concorrentes)

            noticias.append({
                "id": gerar_id(url),
                "titulo": titulo,
                "resumo": resumo,
                "url": url,
                "fonte": fonte["nome"],
                "fonte_url": fonte["url"],
                "tags": tags,
                "categoria": tags[0],           # tag primária para compatibilidade
                "data": data,
                "data_coleta": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            })
    except Exception as e:
        print(f"    ⚠ Erro: {e}")
        return noticias

    print(f"    ✓ {len(noticias)} notícias")
    return noticias

# ── Mesclar sem duplicatas + limpar histórico ─────────────────────────────────
def mesclar(existentes: list, novas: list, dias_historico: int) -> tuple[list, int]:
    ids = {n["id"] for n in existentes}
    adicionadas = 0

    for n in novas:
        if n["id"] not in ids:
            # Retrocompatibilidade: garantir campo tags em registros antigos
            if "tags" not in n:
                n["tags"] = [n.get("categoria", "Novidades no Setor")]
            existentes.append(n)
            ids.add(n["id"])
            adicionadas += 1

    # Garantir campo tags em registros existentes mais antigos
    for n in existentes:
        if "tags" not in n:
            n["tags"] = [n.get("categoria", "Novidades no Setor")]

    existentes.sort(key=lambda n: n["data"], reverse=True)

    corte = datetime.now(timezone.utc) - timedelta(days=dias_historico)
    existentes = [
        n for n in existentes
        if datetime.fromisoformat(n["data"].replace("Z", "+00:00")) >= corte
    ]

    return existentes, adicionadas

# ── Git push ──────────────────────────────────────────────────────────────────
def git_push(token: str, usuario: str, repositorio: str):
    repo_url = f"https://{usuario}:{token}@github.com/{usuario}/{repositorio}.git"
    hoje = datetime.now().strftime("%Y-%m-%d")

    cmds = [
        ["git", "config", "user.email", "coletor@acontece-no-mercado.local"],
        ["git", "config", "user.name", "Coletor Automático"],
        ["git", "add", "data/noticias.json"],
        ["git", "commit", "-m", f"Coleta automática: {hoje}"],
        ["git", "push", repo_url, "HEAD:main"],
    ]

    for cmd in cmds:
        result = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
        stderr = result.stderr.strip()
        if result.returncode != 0 and "nothing to commit" not in (result.stdout + stderr):
            print(f"  ⚠ git {cmd[1]}: {stderr}")
            if cmd[1] == "push":
                raise RuntimeError(f"Push falhou: {stderr}")

# ── Resumo por categoria ─────────────────────────────────