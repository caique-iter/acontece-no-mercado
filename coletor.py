#!/usr/bin/env python3
"""
Coletor de Notícias — Acontece no Mercado
Busca notícias via RSS das fontes configuradas, categoriza e atualiza o histórico.
"""

import json
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import feedparser
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "feedparser", "-q"])
    import feedparser

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "beautifulsoup4", "-q"])
    import requests
    from bs4 import BeautifulSoup

# ── Caminhos ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "data" / "noticias.json"

# ── Carregar configuração ─────────────────────────────────────────────────────
def carregar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

# ── Carregar/salvar dados ─────────────────────────────────────────────────────
def carregar_dados():
    if DATA_PATH.exists():
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"noticias": [], "ultima_atualizacao": None, "total": 0}

def salvar_dados(dados):
    DATA_PATH.parent.mkdir(exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

# ── Gerar ID único por URL ────────────────────────────────────────────────────
def gerar_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:12]

# ── Categorizar notícia ───────────────────────────────────────────────────────
def categorizar(titulo, resumo, categorias):
    texto = (titulo + " " + resumo).lower()
    for cat in categorias:
        for palavra in cat["palavras_chave"]:
            if palavra.lower() in texto:
                return cat["nome"]
    return "Geral"

# ── Coletar via RSS ───────────────────────────────────────────────────────────
def coletar_rss(fonte, categorias, max_noticias):
    noticias = []
    if not fonte.get("rss"):
        return noticias

    print(f"  → Coletando RSS: {fonte['nome']}")
    try:
        feed = feedparser.parse(fonte["rss"])
        for entry in feed.entries[:max_noticias]:
            titulo = entry.get("title", "").strip()
            url = entry.get("link", "").strip()
            resumo = entry.get("summary", entry.get("description", "")).strip()
            # Limpar HTML do resumo
            if "<" in resumo:
                resumo = BeautifulSoup(resumo, "html.parser").get_text()
            resumo = resumo[:300].strip()

            pub_date = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub_date:
                data = datetime(*pub_date[:6], tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                data = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            if not titulo or not url:
                continue

            noticias.append({
                "id": gerar_id(url),
                "titulo": titulo,
                "resumo": resumo,
                "url": url,
                "fonte": fonte["nome"],
                "fonte_url": fonte["url"],
                "categoria": categorizar(titulo, resumo, categorias),
                "data": data,
                "data_coleta": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            })
    except Exception as e:
        print(f"    ⚠ Erro ao coletar {fonte['nome']}: {e}")

    print(f"    ✓ {len(noticias)} notícias coletadas")
    return noticias

# ── Deduplicar e mesclar ──────────────────────────────────────────────────────
def mesclar(existentes, novas, dias_historico):
    ids_existentes = {n["id"] for n in existentes}
    adicionadas = 0

    for noticia in novas:
        if noticia["id"] not in ids_existentes:
            existentes.append(noticia)
            ids_existentes.add(noticia["id"])
            adicionadas += 1

    # Ordenar por data decrescente
    existentes.sort(key=lambda n: n["data"], reverse=True)

    # Manter apenas os últimos N dias
    from datetime import timedelta
    corte = datetime.now(timezone.utc) - timedelta(days=dias_historico)
    existentes = [
        n for n in existentes
        if datetime.fromisoformat(n["data"].replace("Z", "+00:00")) >= corte
    ]

    return existentes, adicionadas

# ── Git: commit e push ────────────────────────────────────────────────────────
def git_push(token, usuario, repositorio):
    repo_url = f"https://{usuario}:{token}@github.com/{usuario}/{repositorio}.git"
    hoje = datetime.now().strftime("%Y-%m-%d")

    cmds = [
        ["git", "config", "user.email", "coletor@acontece-no-mercado.local"],
        ["git", "config", "user.name", "Coletor Automático"],
        ["git", "add", "data/noticias.json"],
        ["git", "commit", "-m", f"Coleta automática: {hoje}"],
        ["git", "push", repo_url, "HEAD:main"]
    ]

    for cmd in cmds:
        result = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
        if result.returncode != 0 and "nothing to commit" not in result.stdout + result.stderr:
            print(f"  ⚠ Git: {' '.join(cmd[:2])} → {result.stderr.strip()}")
            if cmd[1] == "push":
                raise RuntimeError(f"Falha no push: {result.stderr}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("⚠ GITHUB_TOKEN não definido. O push será pulado.")

    print("=" * 50)
    print(f"Acontece no Mercado — Coleta {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    config = carregar_config()
    dados = carregar_dados()
    categorias = config["categorias"]
    cfg = config["configuracoes"]

    fontes_ativas = [f for f in config["fontes"] if f.get("ativo", True)]
    print(f"\n📡 Fontes ativas: {len(fontes_ativas)}")

    todas_novas = []
    for fonte in fontes_ativas:
        novas = coletar_rss(fonte, categorias, cfg["max_noticias_por_coleta"])
        todas_novas.extend(novas)

    noticias_atualizadas, adicionadas = mesclar(
        dados["noticias"], todas_novas, cfg["dias_historico"]
    )

    dados["noticias"] = noticias_atualizadas
    dados["ultima_atualizacao"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dados["total"] = len(noticias_atualizadas)

    salvar_dados(dados)
    print(f"\n✅ {adicionadas} notícias novas | {dados['total']} no histórico total")

    if token:
        print("\n📤 Enviando para GitHub...")
        git_push(token, cfg["github_usuario"], cfg["github_repositorio"])
        print("✅ Push concluído!")

    print("=" * 50)

if __name__ == "__main__":
    main()
