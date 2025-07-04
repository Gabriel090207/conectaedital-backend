from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import pdfplumber
from urllib.parse import urljoin
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import firebase_admin
from firebase_admin import credentials, firestore
import re
from fastapi_utils.tasks import repeat_every
import os
import uvicorn

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inicializa Firebase
cred = credentials.Certificate("chave-firebase.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

def enviar_email(destinatario, assunto, corpo, remetente, senha):
    msg = MIMEMultipart("alternative")
    msg['From'] = remetente
    msg['To'] = destinatario
    msg['Subject'] = assunto
    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif;">
        <div style="background-color:#007BFF;color:white;padding:10px;">
          <h2>Radar Edital</h2>
        </div>
        <div style="padding:10px;">
          <p>{corpo}</p>
          <a href="{corpo.split('Link: ')[-1]}" style="background-color:#28a745;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;">Clique aqui para conferir</a>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, 'html'))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(remetente, senha)
        server.send_message(msg)
        server.quit()
        print(f"✅ Email enviado para {destinatario} - Assunto: {assunto}")
    except Exception as e:
        print(f"❌ Erro ao enviar email para {destinatario}: {e}")

def baixar_pdfs_recentes(url_base, n=5):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    }
    resposta = requests.get(url_base, headers=headers)
    resposta.raise_for_status()
    soup = BeautifulSoup(resposta.text, "html.parser")
    links_pdf = [urljoin(url_base, a['href']) for a in soup.find_all('a', href=True) if a['href'].endswith('.pdf')]
    return links_pdf[:n]

def baixar_pdf(pdf_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/pdf, application/x-download, application/octet-stream",
        "Referer": "https://www.sumare.sp.gov.br/",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Connection": "keep-alive",
    }
    resposta_pdf = requests.get(pdf_url, headers=headers)
    resposta_pdf.raise_for_status()
    with open("ultimo.pdf", "wb") as f:
        f.write(resposta_pdf.content)

    return "ultimo.pdf"

def normalizar_texto(texto):
    return re.sub(r'\s+', '', texto).lower()

def extrair_posicao(arquivo_pdf, cpf, nome, palavra_chave_complexa):
    with pdfplumber.open(arquivo_pdf) as pdf:
        texto_completo = "\n".join(page.extract_text() or "" for page in pdf.pages).lower()

    palavra_chave_norm = palavra_chave_complexa.lower()
    cpf_norm = re.sub(r'\D', '', cpf)  # somente números do CPF
    nome_norm = normalizar_texto(nome)

    if palavra_chave_norm in texto_completo:
        linhas = texto_completo.split('\n')
        for linha in linhas:
            linha_baixa = linha.lower()
            linha_sem_espaco = normalizar_texto(linha)
            linha_cpf = re.sub(r'\D', '', linha)

            # Verifica se CPF e Nome estão na mesma linha
            if cpf_norm in linha_cpf and nome_norm in linha_sem_espaco:
                posicoes = re.findall(r'\b(\d{1,4})\b', linha_baixa)
                if posicoes:
                    return int(posicoes[-1])  # posição encontrada
    return None

def procurar_palavra_simples(texto, palavra):
    return palavra.lower() in texto.lower()

def executar_monitoramento():
    remetente = "testeconcursopdf@gmail.com"
    senha = "jfjriwzzbzqgputl"  # Troque pela sua senha app do Gmail

    usuarios = db.collection("usuarios").stream()
    for doc in usuarios:
        dados = doc.to_dict()
        usuario_id = doc.id
        link = dados.get("link")
        email_destino = dados.get("email")
        palavra_chave_simples = dados.get("palavra_chave_simples")
        palavra_chave_complexa = dados.get("palavra_chave_complexa")
        nome = dados.get("nome")
        cpf = dados.get("cpf")
        ultimo_pdf_url = dados.get("ultimo_pdf")

        if not link or not email_destino:
            print(f"⚠ Dados incompletos para {usuario_id}, ignorando...")
            continue

        try:
            pdfs_recentes = baixar_pdfs_recentes(link, n=5)
            for pdf_url in pdfs_recentes:
                if pdf_url == ultimo_pdf_url:
                    break  # PDF já processado

                print(f"🔔 Verificando PDF: {pdf_url} para {usuario_id}")
                arquivo = baixar_pdf(pdf_url)

                texto_pdf = "\n".join(page.extract_text() or "" for page in pdfplumber.open(arquivo).pages).lower()

                # Busca simples
                if palavra_chave_simples and procurar_palavra_simples(texto_pdf, palavra_chave_simples):
                    corpo = f"Encontramos um novo PDF no site {link} com a palavra-chave simples '{palavra_chave_simples}'. Link: {pdf_url}"
                    enviar_email(email_destino, f"Novo PDF com '{palavra_chave_simples}'", corpo, remetente, senha)

                # Busca combinada (CPF e Nome)
                if palavra_chave_complexa and nome and cpf:
                    posicao = extrair_posicao(arquivo, cpf, nome, palavra_chave_complexa)
                    if posicao is not None:
                        corpo = f"Olá {nome}! Encontramos sua posição no PDF do site {link}. Você ficou em {posicao}º lugar. Link: {pdf_url}"
                        enviar_email(email_destino, f"Posição encontrada: {posicao}º lugar", corpo, remetente, senha)

                # Atualiza o último PDF processado
                db.collection("usuarios").document(usuario_id).update({"ultimo_pdf": pdf_url})
                break  # Processa só o PDF mais recente novo

        except Exception as e:
            print(f"❌ Erro no monitoramento para {usuario_id}: {e}")

# Executa a cada 30 segundos
@app.on_event("startup")
@repeat_every(seconds=30)
def monitoramento_periodico():
    print("🔄 Monitoramento automático iniciado")
    executar_monitoramento()

@app.get("/monitorar")
def monitorar():
    executar_monitoramento()
    return {"status": "OK", "msg": "Monitoramento concluído"}

class UsuarioInput(BaseModel):
    email: str
    link: str
    nome: str
    cpf: str
    palavra_chave_simples: str = None
    palavra_chave_complexa: str = None

@app.post("/cadastrar_usuario")
async def cadastrar_usuario(dados: UsuarioInput):
    db.collection("usuarios").document(dados.email).set({
        "email": dados.email,
        "link": dados.link,
        "nome": dados.nome,
        "cpf": dados.cpf,
        "palavra_chave_simples": dados.palavra_chave_simples,
        "palavra_chave_complexa": dados.palavra_chave_complexa,
        "ultimo_pdf": None
    })
    return {"msg": "Cadastro realizado com sucesso!"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
