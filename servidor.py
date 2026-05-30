#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
servidor.py
============
Trabalho Final — Introdução à Inteligência Artificial

Servidor Web que serve a interface e processa os pedidos de análise alimentar.

PORQUÊ UM SERVIDOR BACKEND?
    A API da Anthropic não pode ser chamada directamente do browser por uma
    razão de segurança chamada CORS (Cross-Origin Resource Sharing): os browsers
    bloqueiam pedidos HTTP para domínios externos feitos por JavaScript.

    Além disso, a API Key nunca deve ficar exposta no código HTML/JavaScript
    (qualquer pessoa que abrisse o código fonte ficaria com a chave).

    A solução é a arquitectura Cliente-Servidor:
        Browser (HTML/JS)  ──POST /analisar──►  Flask (Python)  ──►  Anthropic API
        ◄──────────────────────────────JSON──────────────────────────────────────

TEORIA — Saídas Estruturadas (Structured Outputs):
    Em vez de pedir ao LLM para "descrever" o resultado em texto livre,
    usamos um System Prompt que instrui o modelo a responder em JSON.
    Isto garante que a resposta é sempre interpretável pela aplicação.

    Para maior robustez, o prompt inclui o esquema JSON exacto esperado —
    o modelo aprende por "in-context learning" (aprendizagem no contexto)
    o formato desejado sem necessitar de fine-tuning.

Executar:
    pip install flask anthropic
    $env:ANTHROPIC_API_KEY='sk-ant-...'
    python servidor.py
    Abrir: http://localhost:5000
"""

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_file
import anthropic

# =============================================================================
# CONFIGURAÇÃO DO SERVIDOR FLASK
# =============================================================================
# Flask é um micro-framework web em Python. "Micro" porque não inclui
# funcionalidades complexas como ORM ou validação de formulários por omissão —
# é simples, legível e ideal para APIs pequenas como esta.

app = Flask(__name__)

# Pasta onde se encontra este ficheiro (para servir o HTML)
PASTA_BASE = Path(__file__).parent


# =============================================================================
# SYSTEM PROMPT PARA SAÍDA JSON
# =============================================================================
# Este prompt instrui o modelo a responder SEMPRE com JSON estruturado.
# A diferença em relação ao assistente CLI é o formato de resposta:
# aqui precisamos de dados estruturados para a interface web os apresentar.

SYSTEM_PROMPT_JSON = """És um assistente especializado em segurança alimentar para pessoas com alergias e doença celíaca.

Quando recebes uma imagem de rótulo, extrais todos os ingredientes visíveis e analisas-os.
Quando recebes texto, analisas os ingredientes diretamente.

Responde APENAS com um objeto JSON válido, sem qualquer texto antes ou depois, sem backticks markdown, sem comentários. O JSON deve ter exactamente esta estrutura:

{
  "gluten_verdict": "SEGURO" | "RISCO" | "CONTÉM",
  "ingredients": [
    { "name": "nome do ingrediente", "status": "SEGURO" | "RISCO" | "CONTÉM" | "NEUTRO" }
  ],
  "allergens": [
    { "name": "Nome do alergéneo", "icon": "emoji", "source": "ingrediente responsável" }
  ],
  "notes": "Texto corrido com avisos importantes, em português simples e claro, máximo 3 frases."
}

Regras para glúten:
- CONTÉM: trigo, centeio, cevada, espelta, kamut, triticale, aveia não certificada, farinha sem especificar origem, sêmola, semolina, glúten, bulgur, farro, malte de cevada
- RISCO: amido sem origem declarada, proteína vegetal sem especificar, malte sem origem, aveia sem certificação SG, especiarias com aglutinantes não declarados
- SEGURO: arroz, milho, batata, mandioca, tapioca, quinoa, soja, trigo-sarraceno, teff, ingredientes claramente não-cereais

Alergéneos obrigatórios EU (Regulamento 1169/2011) — verifica todos 14:
- Cereais com glúten (trigo, centeio, cevada, aveia, espelta, kamut) → icon 🌾
- Crustáceos → icon 🦐
- Ovos → icon 🥚
- Peixe → icon 🐟
- Amendoins → icon 🥜
- Soja → icon 🫘
- Leite (incluindo lactose, manteiga, soro) → icon 🥛
- Frutos de casca rija (amêndoas, nozes, avelãs, cajus, pecãs, pistácios, macadâmia, noz do Brasil) → icon 🌰
- Aipo → icon 🥬
- Mostarda → icon 🌿
- Sementes de sésamo → icon 🌱
- Dióxido de enxofre e sulfitos (>10mg/kg) → icon 🧪
- Tremoço → icon 🫛
- Moluscos → icon 🐚

Regras gerais:
- Sê sempre conservador: em caso de dúvida, classifica como RISCO
- Nunca declares um produto SEGURO sem certeza absoluta
- Inclui nas notas alertas sobre ingredientes ambíguos ou contaminação cruzada
- Inclui apenas alergéneos efectivamente detetados na lista "allergens"
- Se nenhum alergéneo for encontrado, retorna uma lista vazia: "allergens": []"""


# =============================================================================
# ROTAS DO SERVIDOR
# =============================================================================

@app.route("/")
def pagina_inicial():
    """
    Serve o ficheiro HTML da interface web.

    TEORIA — Servidor de Ficheiros Estáticos:
    Quando o browser acede a http://localhost:5000/, o servidor Flask
    responde com o conteúdo do ficheiro interface_web.html. O browser
    interpreta o HTML, CSS e JavaScript e apresenta a interface.
    """
    caminho_html = PASTA_BASE / "interface_web.html"
    if not caminho_html.exists():
        return "Erro: interface_web.html não encontrado.", 404
    return send_file(str(caminho_html))
@app.route("/App_image.jpg")
def imagem_header():
    return send_file(str(PASTA_BASE / "App_image.jpg"))

@app.route("/analisar", methods=["POST"])
def analisar():
    """
    Endpoint de análise: recebe ingredientes e devolve análise em JSON.

    TEORIA — API REST:
    REST (Representational State Transfer) é um estilo de arquitectura para
    APIs web. Neste endpoint:
        - URL:    /analisar
        - Método: POST (envio de dados para processamento)
        - Input:  JSON com { texto, imagem_base64, tipo_media }
        - Output: JSON com { gluten_verdict, ingredients, allergens, notes }

    O método POST é usado (em vez de GET) porque:
        1. Enviamos dados (ingredientes/imagem) — não apenas pedimos recursos
        2. O POST body pode conter dados grandes (imagens base64)
        3. GET requests ficam no histórico do browser — POST não

    TEORIA — Stateless (Sem Estado):
    Cada pedido a /analisar é independente. Não há sessão ou memória entre
    pedidos. O frontend envia sempre todos os dados necessários (texto e/ou
    imagem), e o servidor processa de raiz. Isto simplifica a escalabilidade.
    """
    # Obter dados JSON enviados pelo browser
    dados = request.get_json()
    if not dados:
        return jsonify({"erro": "Pedido inválido: JSON esperado"}), 400

    texto        = dados.get("texto", "").strip()
    imagem_base64 = dados.get("imagem_base64")
    tipo_media   = dados.get("tipo_media", "image/jpeg")

    # Validar: pelo menos texto ou imagem devem ser fornecidos
    if not texto and not imagem_base64:
        return jsonify({"erro": "Nenhum ingrediente fornecido"}), 400

    # -----------------------------------------------------------------
    # Construir o conteúdo da mensagem (texto e/ou imagem)
    # -----------------------------------------------------------------
    conteudo = []

    if imagem_base64:
        # Bloco de imagem: codificação base64 com tipo MIME correcto
        conteudo.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": tipo_media,
                "data": imagem_base64,
            },
        })

    # Instrução de análise adaptada ao input disponível
    if imagem_base64 and texto:
        instrucao = f"Analisa os ingredientes desta imagem de rótulo e/ou do seguinte texto: {texto}"
    elif imagem_base64:
        instrucao = "Analisa os ingredientes visíveis nesta imagem de rótulo alimentar."
    else:
        instrucao = f"Analisa os seguintes ingredientes: {texto}"

    conteudo.append({"type": "text", "text": instrucao})

    # -----------------------------------------------------------------
    # Chamar a API da Anthropic
    # -----------------------------------------------------------------
    try:
        # O cliente lê automaticamente ANTHROPIC_API_KEY do ambiente
        cliente = anthropic.Anthropic()

        resposta = cliente.messages.create(
            model="claude-opus-4-7",  # Modelo mais capaz da família Claude 4
            max_tokens=1024,           # Resposta JSON compacta não precisa de muito
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT_JSON,
                    # Prompt Caching: o system prompt é cacheado pelo API
                    # Nas chamadas seguintes, estes tokens custam ~10% do normal
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": conteudo}],
        )

        # Extrair texto da resposta
        texto_resposta = "".join(
            bloco.text for bloco in resposta.content if bloco.type == "text"
        )

        # Limpar markdown caso o modelo o inclua por engano
        texto_limpo = texto_resposta.replace("```json", "").replace("```", "").strip()

        # Converter string JSON → dicionário Python → JSON HTTP
        resultado = json.loads(texto_limpo)
        return jsonify(resultado)

    except anthropic.AuthenticationError:
        return jsonify({"erro": "API Key inválida. Verifica ANTHROPIC_API_KEY."}), 401

    except anthropic.RateLimitError:
        return jsonify({"erro": "Limite de pedidos atingido. Tenta novamente em breve."}), 429

    except anthropic.APIConnectionError:
        return jsonify({"erro": "Erro de ligação à API. Verifica a internet."}), 503

    except json.JSONDecodeError:
        # O modelo não devolveu JSON válido — erro raro mas possível
        return jsonify({"erro": "Resposta inválida do modelo. Tenta novamente."}), 500

    except Exception as erro:
        return jsonify({"erro": f"Erro interno: {str(erro)}"}), 500


# =============================================================================
# PONTO DE ENTRADA
# =============================================================================

def main():
    """Verifica requisitos e inicia o servidor."""
    # Verificar API Key antes de iniciar
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("=" * 55)
        print("❌ ANTHROPIC_API_KEY não está definida.")
        print("=" * 55)
        print("\nDefine a variável de ambiente e reinicia:")
        print("  Linux/macOS:  export ANTHROPIC_API_KEY='sk-ant-...'")
        print("  Windows PS:   $env:ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    # Verificar que o HTML existe
    if not (PASTA_BASE / "interface_web.html").exists():
        print("⚠️  Aviso: interface_web.html não encontrado na pasta.")

    print("\n" + "=" * 55)
    print("🌿 ASSISTENTE DE SEGURANÇA ALIMENTAR — SERVIDOR WEB")
    print("=" * 55)
    print("📡 A iniciar servidor em http://localhost:5000")
    print("   Pressiona Ctrl+C para parar\n")

    # debug=False em produção; debug=True recarrega automaticamente
    # ao guardar ficheiros (útil durante desenvolvimento)
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    main()
