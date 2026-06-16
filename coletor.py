#!/usr/bin/env python3
"""Coletor de Noticias - Acontece no Mercado (v5 - exige_relevancia por categoria, aceitar_tudo por fonte, _score nos artigos)"""

import json, hashlib, os, subprocess, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

def pip_install(*pkgs):
    subprocess.check_call([sys.executable,"-m","pip","install",*pkgs,"-q","--break-system-packages"])

try: import feedparser
except ImportError: pip_install("feedparser"); import feedparser

try: import requests; from bs4 import BeautifulSoup
except ImportError: pip_install("requests","beautifulsoup4"); import requests; from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "data" / "noticias.json"

def carregar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f: return json.load(f)

def carregar_dados():
    if DATA_PATH.exists():
        with open(DATA_PATH, encoding="utf-8") as f: return json.load(f)
    return {"noticias":[],"ultima_atualizacao":None,"total":0}

def salvar_dados(dados):
    DATA_PATH.parent.mkdir(exist_ok=True)
    with open(DATA_PATH,"w",encoding="utf-8") as f: json.dump(dados,f,ensure_ascii=False,indent=2)

def gerar_id(url): return hashlib.md5(url.encode()).hexdigest()[:12]

def normalizar(texto):
    import re as _re
    t = texto.lower()
    subs = [("ã","a"),("â","a"),("á","a"),("à","a"),("ê","e"),("é","e"),("è","e"),
            ("î","i"),("í","i"),("õ","o"),("ô","o"),("ó","o"),("ò","o"),
            ("ú","u"),("û","u"),("ù","u"),("ç","c"),("ñ","n")]
    for orig, rep in subs: t = t.replace(orig, rep)
    # remover pontuação especial (aspas, apostrofe, traço) sem apagar espaços
    t = _re.sub(r"['\-]", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t

def e_relevante_turismo(texto_norm, termos):
    return any(normalizar(t) in texto_norm for t in termos)

def detectar_concorrente(texto_norm, concorrentes):
    todos = concorrentes.get("rj",[]) + concorrentes.get("rs",[])
    return any(normalizar(c) in texto_norm for c in todos)

def taguear_com_score(titulo, resumo, categorias, concorrentes, max_tags,
                      termos_relevancia, especializada):
    """
    v5: suporte a exige_relevancia por categoria.
    - Se categoria tem exige_relevancia=true, o artigo precisa ter pelo menos
      um termo do filtro_relevancia para se qualificar nessa categoria,
      mesmo vindo de fonte especializada.
    - Retorna (tags, score_total). Score=0 indica descarte.
    """
    texto_norm = normalizar(titulo + " " + resumo)
    tem_relevancia = e_relevante_turismo(texto_norm, termos_relevancia)
    scores = {}

    # Concorrentes: prioridade absoluta via lista dedicada
    if detectar_concorrente(texto_norm, concorrentes):
        scores["Concorrentes"] = 999

    # Avaliar todas as categorias (incluindo Novidades no Setor)
    for cat in categorias:
        nome = cat["nome"]
        if nome == "Concorrentes": continue
        frases = cat.get("frases", cat.get("palavras_chave", []))
        score_min = cat.get("score_minimo", 1)
        exige_rel = cat.get("exige_relevancia", False)

        # Categoria com exige_relevancia: artigo deve ter termo turístico
        if exige_rel and not tem_relevancia:
            continue

        score = sum(1 for f in frases if normalizar(f) in texto_norm)
        if score >= score_min:
            scores[nome] = score

    if not scores:
        return [], 0  # nenhuma categoria encaixou: descartar

    ordenado = sorted(scores.items(), key=lambda x: -x[1])
    score_total = sum(scores.values())
    return [tag for tag, _ in ordenado[:max_tags]], score_total

def limpar_html(texto, max_chars=300):
    if "<" in texto: texto = BeautifulSoup(texto,"html.parser").get_text(separator=" ")
    return " ".join(texto.split())[:max_chars].strip()

def extrair_imagem(entry):
    """Tenta extrair URL de imagem do RSS entry (media, enclosure ou img no summary)."""
    try:
        # 1. media:thumbnail
        mt = getattr(entry, "media_thumbnail", None)
        if mt and isinstance(mt, list):
            url = mt[0].get("url", "")
            if url.startswith("http"): return url
        # 2. media:content
        mc = getattr(entry, "media_content", None)
        if mc and isinstance(mc, list):
            for m in mc:
                url = m.get("url", "")
                medium = m.get("medium", "")
                if url.startswith("http") and ("image" in medium or
                   any(url.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".webp"))):
                    return url
        # 3. enclosures
        for enc in getattr(entry, "enclosures", []):
            if "image" in enc.get("type", ""):
                url = enc.get("href", enc.get("url", ""))
                if url.startswith("http"): return url
        # 4. img tag no summary/content
        html = entry.get("summary", "")
        if not html and entry.get("content"):
            html = entry["content"][0].get("value", "")
        if "<img" in html:
            soup = BeautifulSoup(html, "html.parser")
            img = soup.find("img")
            if img:
                url = img.get("src", img.get("data-src", ""))
                if url and url.startswith("http"): return url
    except Exception:
        pass
    return None

def coletar_rss(fonte, categorias, concorrentes, termos_relevancia, max_n, max_tags):
    noticias = []
    if not fonte.get("rss"): return noticias
    especializada = fonte.get("especializada", True)
    aceitar_tudo = fonte.get("aceitar_tudo", False)
    padroes_exclusao = [normalizar(p) for p in fonte.get("padroes_exclusao", [])]
    modo = "[catch-all]" if aceitar_tudo else ("[esp]" if especializada else "[gen]")
    print(f"  -> {fonte['nome']} {modo}")
    descartadas_relevancia = 0
    descartadas_categoria = 0
    descartadas_exclusao = 0
    try:
        feed = feedparser.parse(fonte["rss"])
        for entry in feed.entries[:max_n]:
            titulo = entry.get("title","").strip()
            url = entry.get("link","").strip()
            if not titulo or not url: continue
            resumo = limpar_html(entry.get("summary", entry.get("description","")))

            texto_norm = normalizar(titulo + " " + resumo)

            # Padrões de exclusão (ex: notícias internas de gestão/RH)
            if padroes_exclusao and any(p in texto_norm for p in padroes_exclusao):
                descartadas_exclusao += 1
                continue

            # Filtro de relevância: fontes generalistas precisam de termo turístico
            if not especializada and not aceitar_tudo:
                if not e_relevante_turismo(texto_norm, termos_relevancia):
                    descartadas_relevancia += 1
                    continue

            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub: data = datetime(*pub[:6],tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else: data = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            tags, score = taguear_com_score(titulo, resumo, categorias, concorrentes, max_tags,
                                            termos_relevancia, especializada)

            # aceitar_tudo: se nenhuma categoria encaixou, usa "Novidades no Setor"
            if not tags:
                if aceitar_tudo:
                    tags = ["Novidades no Setor"]
                    score = 1
                else:
                    descartadas_categoria += 1
                    continue

            # Bônus de score: imagem disponível (+2), fonte especializada (+1)
            imagem = extrair_imagem(entry)
            if imagem: score += 2
            if especializada or aceitar_tudo: score += 1

            noticias.append({"id":gerar_id(url),"titulo":titulo,"resumo":resumo,"url":url,
                "imagem":imagem,"fonte":fonte["nome"],"fonte_url":fonte["url"],"tags":tags,
                "categoria":tags[0],"data":data,"_score":score,
                "data_coleta":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    except Exception as e: print(f"    aviso: {e}")

    partes = [f"{len(noticias)} aceitas"]
    if descartadas_relevancia: partes.append(f"{descartadas_relevancia} sem relevancia")
    if descartadas_categoria: partes.append(f"{descartadas_categoria} sem categoria")
    if descartadas_exclusao: partes.append(f"{descartadas_exclusao} excluidas")
    print(f"    {' | '.join(partes)}")
    return noticias

def mesclar(existentes, novas, dias_historico):
    ids = {n["id"] for n in existentes}
    adicionadas = 0
    for n in novas:
        if n["id"] not in ids:
            if "tags" not in n: n["tags"] = [n.get("categoria","Novidades no Setor")]
            existentes.append(n); ids.add(n["id"]); adicionadas += 1
    existentes.sort(key=lambda n: n["data"], reverse=True)
    corte = datetime.now(timezone.utc) - timedelta(days=dias_historico)
    existentes = [n for n in existentes if datetime.fromisoformat(n["data"].replace("Z","+00:00")) >= corte]
    return existentes, adicionadas

def git_push(token, usuario, repositorio):
    repo_url = f"https://{usuario}:{token}@github.com/{usuario}/{repositorio}.git"
    hoje = datetime.now().strftime("%Y-%m-%d")
    subprocess.run(["git","pull","--rebase",repo_url,"main"], cwd=BASE_DIR, capture_output=True)
    cmds = [
        ["git","config","user.email","coletor@acontece-no-mercado.local"],
        ["git","config","user.name","Coletor Automatico"],
        ["git","add","data/noticias.json"],
        ["git","commit","-m",f"Coleta automatica: {hoje}"],
        ["git","push",repo_url,"HEAD:main"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout+r.stderr):
            print(f"  aviso git {cmd[1]}: {r.stderr.strip()[:120]}")
            if cmd[1] == "push": raise RuntimeError(f"Push falhou")

def resumo_por_tags(noticias_novas):
    contagem = {}
    for n in noticias_novas:
        for tag in n.get("tags",[n.get("categoria","?")]):
            contagem[tag] = contagem.get(tag,0) + 1
    return "\n".join(f"  {t}: {q}" for t,q in sorted(contagem.items(),key=lambda x:-x[1])) or "  (nenhuma)"

def main():
    token = os.environ.get("GITHUB_TOKEN","")
    print("="*55)
    print(f"Acontece no Mercado - Coleta {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*55)
    config = carregar_config()
    dados = carregar_dados()
    categorias = config["categorias"]
    concorrentes = config.get("concorrentes",{})
    termos_relevancia = config.get("filtro_relevancia",[])
    cfg = config["configuracoes"]
    max_tags = cfg.get("max_tags_por_noticia", 3)

    fontes_ativas = [f for f in config["fontes"] if f.get("ativo",True)]
    esp = sum(1 for f in fontes_ativas if f.get("especializada",True))
    print(f"\nFontes: {esp} especializadas + {len(fontes_ativas)-esp} generalistas")

    todas_novas = []
    for fonte in fontes_ati