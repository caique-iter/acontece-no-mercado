#!/usr/bin/env python3
"""Coletor de Noticias - Acontece no Mercado (v5)

Metodologia:
- Panrotas (aceitar_tudo=true): aceita tudo exceto gestao/reestruturacao.
  Categoriza com frases; se nenhuma casar -> "Novidades no Setor".
- Fontes especializadas (especializada=true): bypass relevancia, exige categoria.
- Fontes generalistas: testa titulo+resumo; se falhar, busca corpo do artigo.
  Categoria obrigatoria (sem fallback).
"""

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
    t = _re.sub(r"['\-]", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t

def e_relevante_turismo(texto_norm, termos):
    return any(normalizar(t) in texto_norm for t in termos)

def detectar_concorrente(texto_norm, concorrentes):
    todos = concorrentes.get("rj",[]) + concorrentes.get("rs",[])
    return any(normalizar(c) in texto_norm for c in todos)

def verificar_exclusao(titulo, resumo, padroes):
    """Retorna True se o artigo deve ser descartado por tratar de gestão/reestruturação."""
    texto_norm = normalizar(titulo + " " + resumo)
    return any(normalizar(p) in texto_norm for p in padroes)

def fetch_corpo_artigo(url, timeout=6, max_chars=2000):
    """Busca o corpo do artigo para enriquecer o filtro de relevancia e classificacao."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AconteceNoMercado/1.0)"}
        r = requests.get(url, timeout=timeout, headers=headers)
        if r.status_code != 200: return ""
        soup = BeautifulSoup(r.text, "html.parser")
        # Remover nav, header, footer, scripts
        for tag in soup(["script","style","nav","header","footer","aside","form"]):
            tag.decompose()
        # Tentar seletores de conteudo principal
        for sel in ["article", "main", ".article-body", ".post-content",
                    ".entry-content", ".materia-corpo", ".noticia-texto", "#conteudo"]:
            el = soup.select_one(sel)
            if el:
                texto = " ".join(el.get_text(separator=" ").split())
                if len(texto) > 100:
                    return texto[:max_chars]
        # Fallback: primeiros paragrafos
        paras = soup.find_all("p")
        texto = " ".join(" ".join(p.get_text().split()) for p in paras[:20])
        return texto[:max_chars]
    except Exception:
        return ""

def taguear_com_score(titulo, resumo, categorias, concorrentes, max_tags, texto_extra=""):
    """
    Classifica o artigo com base em titulo+resumo (+ texto_extra quando disponivel).
    Retorna lista de tags ordenadas por score. Lista vazia = nenhuma categoria encaixou.
    """
    texto_norm = normalizar(titulo + " " + resumo + (" " + texto_extra if texto_extra else ""))
    scores = {}

    if detectar_concorrente(texto_norm, concorrentes):
        scores["Concorrentes"] = 999

    for cat in categorias:
        nome = cat["nome"]
        if nome == "Concorrentes": continue
        frases = cat.get("frases", cat.get("palavras_chave", []))
        score_min = cat.get("score_minimo", 1)
        score = sum(1 for f in frases if normalizar(f) in texto_norm)
        if score >= score_min:
            scores[nome] = score

    if not scores:
        return []

    ordenado = sorted(scores.items(), key=lambda x: -x[1])
    return [tag for tag, _ in ordenado[:max_tags]]

def limpar_html(texto, max_chars=400):
    if "<" in texto: texto = BeautifulSoup(texto,"html.parser").get_text(separator=" ")
    return " ".join(texto.split())[:max_chars].strip()

def extrair_imagem(entry):
    """Tenta extrair URL de imagem do RSS entry."""
    try:
        mt = getattr(entry, "media_thumbnail", None)
        if mt and isinstance(mt, list):
            url = mt[0].get("url", "")
            if url.startswith("http"): return url
        mc = getattr(entry, "media_content", None)
        if mc and isinstance(mc, list):
            for m in mc:
                url = m.get("url", "")
                medium = m.get("medium", "")
                if url.startswith("http") and ("image" in medium or
                   any(url.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".webp"))):
                    return url
        for enc in getattr(entry, "enclosures", []):
            if "image" in enc.get("type", ""):
                url = enc.get("href", enc.get("url", ""))
                if url.startswith("http"): return url
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

def calcular_pontuacao(texto_norm, categorias):
    """Soma de frases casadas em todas as categorias (exceto Concorrentes)."""
    total = 0
    for cat in categorias:
        if cat["nome"] == "Concorrentes": continue
        for f in cat.get("frases", []):
            if normalizar(f) in texto_norm:
                total += 1
    return total

def coletar_rss(fonte, categorias, concorrentes, termos_relevancia, max_n, max_tags):
    noticias = []
    if not fonte.get("rss"): return noticias

    especializada  = fonte.get("especializada", True)
    aceitar_tudo   = fonte.get("aceitar_tudo", False)
    padroes_excl   = fonte.get("padroes_exclusao", [])

    modo = "[editorial]" if aceitar_tudo else ("[esp]" if especializada else "[gen]")
    print(f"  -> {fonte['nome']} {modo}")

    desc_exclusao   = 0
    desc_relevancia = 0
    desc_categoria  = 0
    corpo_buscados  = 0

    try:
        feed = feedparser.parse(fonte["rss"])
        for entry in feed.entries[:max_n]:
            titulo = entry.get("title","").strip()
            url    = entry.get("link","").strip()
            if not titulo or not url: continue
            resumo = limpar_html(entry.get("summary", entry.get("description","")))
            corpo  = ""

            # --- MODO EDITORIAL (Panrotas) ---
            if aceitar_tudo:
                if padroes_excl and verificar_exclusao(titulo, resumo, padroes_excl):
                    desc_exclusao += 1
                    continue
                # tenta categorizar; fallback para Novidades no Setor
                tags = taguear_com_score(titulo, resumo, categorias, concorrentes, max_tags)
                if not tags:
                    tags = ["Novidades no Setor"]

            # --- MODO GENERALISTA ---
            elif not especializada:
                texto_n = normalizar(titulo + " " + resumo)
                if not e_relevante_turismo(texto_n, termos_relevancia):
                    # Segunda chance: buscar corpo do artigo
                    corpo = fetch_corpo_artigo(url)
                    corpo_buscados += 1
                    texto_n2 = normalizar(titulo + " " + resumo + " " + corpo)
                    if not e_relevante_turismo(texto_n2, termos_relevancia):
                        desc_relevancia += 1
                        continue
                tags = taguear_com_score(titulo, resumo, categorias, concorrentes, max_tags,
                                         texto_extra=corpo)
                if not tags:
                    desc_categoria += 1
                    continue

            # --- MODO ESPECIALIZADO (normal) ---
            else:
                tags = taguear_com_score(titulo, resumo, categorias, concorrentes, max_tags)
                if not tags:
                    desc_categoria += 1
                    continue

            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub: data = datetime(*pub[:6],tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:   data = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            imagem = extrair_imagem(entry)

            # Pontuação editorial: frases casadas + bônus por tags extras + bônus Panrotas
            _texto_score = normalizar(titulo + " " + resumo + (" " + corpo if corpo else ""))
            _score = calcular_pontuacao(_texto_score, categorias)
            _score += max(0, len(tags) - 1) * 5   # +5 por tag extra
            if imagem: _score += 3                 # +3 tem imagem
            if aceitar_tudo: _score += 20          # +20 fonte editorial (Panrotas)

            noticias.append({
                "id": gerar_id(url), "titulo": titulo, "resumo": resumo,
                "url": url, "imagem": imagem,
                "fonte": fonte["nome"], "fonte_url": fonte["url"],
                "tags": tags, "categoria": tags[0], "data": data,
                "_score": _score,
                "data_coleta": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            })

    except Exception as e:
        print(f"    aviso: {e}")

    partes = [f"{len(noticias)} aceitas"]
    if desc_exclusao:   partes.append(f"{desc_exclusao} excluidas por gestao")
    if desc_relevancia: partes.append(f"{desc_relevancia} sem relevancia")
    if desc_categoria:  partes.append(f"{desc_categoria} sem categoria")
    if corpo_buscados:  partes.append(f"{corpo_buscados} corpos buscados")
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
    existentes = [n for n in existentes
                  if datetime.fromisoformat(n["data"].replace("Z","+00:00")) >= corte]
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
            if cmd[1] == "push": raise RuntimeError("Push falhou")

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
    config   = carregar_config()
    dados    = carregar_dados()
    cats     = config["categorias"]
    concorr  = config.get("concorrentes",{})
    termos   = config.get("filtro_relevancia",[])
    cfg      = config["configuracoes"]
    max_tags = cfg.get("max_tags_por_noticia", 3)

    fontes_ativas = [f for f in config["fontes"] if f.get("ativo",True)]
    editoriais = sum(1 for f in fontes_ativas if f.get("aceitar_tudo"))
    esp        = sum(1 for f in fontes_ativas if f.get("especializada",True) and not f.get("aceitar_tudo"))
    gen        = len(fontes_ativas) - editoriais - esp
    print(f"\nFontes: {editoriais} editorial | {esp} especializadas | {gen} generalistas")

    todas_novas = []
    for fonte in fontes_ativas:
        novas = coletar_rss(fonte, cats, concorr, termos,
                            cfg["max_noticias_por_coleta"], max_tags)
        todas_novas.extend(novas)

    atualizadas, adicionadas = mesclar(dados["noticias"], todas_novas, cfg["dias_historico"])
    dados["noticias"]          = atualizadas
    dados["ultima_atualizacao"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dados["total"]             = len(atualizadas)
    salvar_dados(dados)

    ids_novos = {n["id"] for n in todas_novas}
    novas_add = [n for n in atualizadas if n["id"] in ids_novos][:adicionadas]

    print(f"\n{adicionadas} noticias novas | {dados['total']} no historico")
    print(f"\nPor categoria:\n{resumo_por_tags(novas_add)}")

    rivais = [n for n in novas_add if "Concorrentes" in n.get("tags",[])]
    if rivais:
        print(f"\n*** {len(rivais)} mencao(oes) a concorrentes:")
        for n in rivais: print(f"  - {n['titulo'][:70]} [{n['fonte']}]")

    if token:
        print("\nPublicando no GitHub Pages...")
        git_push(token, cfg["github_usuario"], cfg["github_repositorio"])
        print("Site atualizado!")
    else:
        print("\nGITHUB_TOKEN nao definido.")
    print("="*55)

if __name__ == "__main__":
    main()
