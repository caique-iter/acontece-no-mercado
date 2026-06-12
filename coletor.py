#!/usr/bin/env python3
"""Coletor de Noticias - Acontece no Mercado"""

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

def detectar_concorrente(texto, concorrentes):
    t = texto.lower()
    todos = concorrentes.get("rj",[]) + concorrentes.get("rs",[])
    return any(c.lower() in t for c in todos)

def taguear(titulo, resumo, categorias, concorrentes):
    texto = (titulo + " " + resumo).lower()
    tags = []
    if detectar_concorrente(texto, concorrentes):
        tags.append("Concorrentes")
    for cat in categorias:
        if cat["nome"] == "Concorrentes": continue
        for palavra in cat.get("palavras_chave",[]):
            if palavra.lower() in texto:
                if cat["nome"] not in tags: tags.append(cat["nome"])
                break
    return tags if tags else ["Novidades no Setor"]

def limpar_html(texto, max_chars=300):
    if "<" in texto: texto = BeautifulSoup(texto,"html.parser").get_text(separator=" ")
    return " ".join(texto.split())[:max_chars].strip()

def coletar_rss(fonte, categorias, concorrentes, max_n):
    noticias = []
    if not fonte.get("rss"): return noticias
    print(f"  -> {fonte['nome']}")
    try:
        feed = feedparser.parse(fonte["rss"])
        for entry in feed.entries[:max_n]:
            titulo = entry.get("title","").strip()
            url = entry.get("link","").strip()
            if not titulo or not url: continue
            resumo = limpar_html(entry.get("summary", entry.get("description","")))
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub: data = datetime(*pub[:6],tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else: data = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            tags = taguear(titulo, resumo, categorias, concorrentes)
            noticias.append({"id":gerar_id(url),"titulo":titulo,"resumo":resumo,"url":url,
                "fonte":fonte["nome"],"fonte_url":fonte["url"],"tags":tags,"categoria":tags[0],
                "data":data,"data_coleta":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    except Exception as e: print(f"    aviso: {e}")
    print(f"    ok: {len(noticias)} noticias")
    return noticias

def mesclar(existentes, novas, dias_historico):
    ids = {n["id"] for n in existentes}
    adicionadas = 0
    for n in novas:
        if n["id"] not in ids:
            if "tags" not in n: n["tags"] = [n.get("categoria","Novidades no Setor")]
            existentes.append(n); ids.add(n["id"]); adicionadas += 1
    for n in existentes:
        if "tags" not in n: n["tags"] = [n.get("categoria","Novidades no Setor")]
    existentes.sort(key=lambda n: n["data"], reverse=True)
    corte = datetime.now(timezone.utc) - timedelta(days=dias_historico)
    existentes = [n for n in existentes if datetime.fromisoformat(n["data"].replace("Z","+00:00")) >= corte]
    return existentes, adicionadas

def git_push(token, usuario, repositorio):
    repo_url = f"https://{usuario}:{token}@github.com/{usuario}/{repositorio}.git"
    hoje = datetime.now().strftime("%Y-%m-%d")
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
            print(f"  aviso git {cmd[1]}: {r.stderr.strip()}")
            if cmd[1] == "push": raise RuntimeError(f"Push falhou: {r.stderr}")

def resumo_por_tags(noticias_novas):
    contagem = {}
    for n in noticias_novas:
        for tag in n.get("tags",[n.get("categoria","?")]):
            contagem[tag] = contagem.get(tag,0) + 1
    return "\n".join(f"  {t}: {q}" for t,q in sorted(contagem.items(),key=lambda x:-x[1])) or "  (nenhuma)"

def main():
    token = os.environ.get("GITHUB_TOKEN","")
    print("="*50)
    print(f"Acontece no Mercado - Coleta {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*50)
    config = carregar_config()
    dados = carregar_dados()
    categorias = config["categorias"]
    concorrentes = config.get("concorrentes",{})
    cfg = config["configuracoes"]
    fontes_ativas = [f for f in config["fontes"] if f.get("ativo",True)]
    print(f"\nFontes ativas: {len(fontes_ativas)}")
    todas_novas = []
    for fonte in fontes_ativas:
        novas = coletar_rss(fonte, categorias, concorrentes, cfg["max_noticias_por_coleta"])
        todas_novas.extend(novas)
    atualizadas, adicionadas = mesclar(dados["noticias"], todas_novas, cfg["dias_historico"])
    dados["noticias"] = atualizadas
    dados["ultima_atualizacao"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dados["total"] = len(atualizadas)
    salvar_dados(dados)
    print(f"\n{adicionadas} noticias novas | {dados['total']} no historico")
    ids_novos = {n["id"] for n in todas_novas}
    novas_add = [n for n in atualizadas if n["id"] in ids_novos][:adicionadas]
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
        print("\nGITHUB_TOKEN nao definido - push pulado.")
    print("="*50)

if __name__ == "__main__":
    main()
