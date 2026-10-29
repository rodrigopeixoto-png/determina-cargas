import streamlit as st
import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.placement
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from openpyxl.drawing.image import Image as OpenpyxlImage
import tempfile
import io
import os
import gc

st.set_page_config(page_title="Descida de Cargas IFC", layout="wide")

st.title("Descida de Cargas para Radier (IFC -> Eberick)")
st.markdown("""
Esta rotina calcula o peso próprio das paredes e distribui as lajes considerando **faixas de influência tributárias reais** (diferenciando paredes internas de fachadas e ignorando lajes apoiadas no solo).
""")

# --- PARÂMETROS ---
st.sidebar.header("Parâmetros de Carga")
peso_alvenaria = st.sidebar.number_input("Peso Específico da Parede (kN/m³)", value=25.0, step=1.0)
peso_concreto = st.sidebar.number_input("Peso Específico da Laje (kN/m³)", value=25.0, step=1.0)
carga_acidental_laje = st.sidebar.number_input("Sobrecarga Acidental na Laje (kN/m²)", value=2.0, step=0.5)

st.sidebar.header("Geometria Tributária")
vao_medio_laje = st.sidebar.number_input("Vão Médio Livre dos Quartos (m)", value=4.15, step=0.05)
exibir_rotulos = st.sidebar.checkbox("Exibir valores numéricos nas paredes", value=True)
tamanho_fonte = st.sidebar.slider("Tamanho da fonte dos rótulos", min_value=5, max_value=11, value=7)
resolucao_grid = st.sidebar.slider("Resolução da Malha Gráfica (m)", 0.1, 0.5, 0.25, 0.05)

arquivo_ifc = st.file_uploader("Anexe o ficheiro IFC estrutural ou arquitetónico", type=["ifc"])

def obter_fator_comprimento(ifc):
    for unit in ifc.by_type("IfcSIUnit"):
        if unit.UnitType == "LENGTHUNIT":
            if unit.Prefix == "MILLI":
                return 0.001
            elif unit.Prefix == "CENTI":
                return 0.01
            elif unit.Prefix == "DECI":
                return 0.1
            elif unit.Prefix is None:
                return 1.0
    return 0.001

def extrair_dimensoes_elemento(elemento, tipo, fator_comp):
    psets = ifcopenshell.util.element.get_psets(elemento) or {}
    qto_nome = "Qto_WallBaseQuantities" if tipo == "IfcWall" else "Qto_SlabBaseQuantities"
    quantities = psets.get(qto_nome, {})
    
    comprimento = quantities.get("Length", 0.0)
    volume = quantities.get("NetVolume", quantities.get("GrossVolume", 0.0))
    area = quantities.get("NetArea", quantities.get("GrossArea", 0.0))
    
    if volume == 0.0 or comprimento == 0.0:
        cotas = psets.get("Cotas", {})
        if comprimento == 0.0: comprimento = cotas.get("Comprimento", 0.0)
        if volume == 0.0: volume = cotas.get("Volume", 0.0)
        if area == 0.0: area = cotas.get("Área", 0.0)
            
    if comprimento > 100.0:
        comprimento = comprimento * fator_comp
        
    return float(comprimento), float(area), float(volume)

def processar_calculo_tributario(caminho_ifc, p_alvenaria, p_concreto, q_acidental, vao, resolucao, rotulos, tam_fonte):
    ifc = ifcopenshell.open(caminho_ifc)
    fator_comp = obter_fator_comprimento(ifc)
    
    # 1. Identificar espessura predominante das lajes elevadas (excluindo piso térreo / radier)
    lajes_elevadas = []
    for s in ifc.by_type("IfcSlab"):
        nome = (s.Name or "").upper()
        # Ignora lajes de piso de fundação/térreo
        if "LAJE_PISO_20CM" in nome or "PISO_20CM" in nome or "RADIER" in nome:
            continue
        lajes_elevadas.append(s)

    # Pressão superficial padrão da laje elevada (kN/m²)
    if lajes_elevadas:
        _, a_tot, v_tot = extrair_dimensoes_elemento(lajes_elevadas[0], "IfcSlab", fator_comp)
        espessura_media = (v_tot / a_tot) if a_tot > 0 else 0.15
    else:
        espessura_media = 0.15

    q_superficial_laje = (espessura_media * p_concreto) + q_acidental

    # 2. Processar paredes do piso térreo
    paredes_terreo = []
    for w in ifc.by_type("IfcWall") + ifc.by_type("IfcWallStandardCase"):
        # Filtra paredes associadas ao Nível 1
        psets = ifcopenshell.util.element.get_psets(w) or {}
        restricoes = psets.get("Restrições", {})
        nivel = restricoes.get("Restrição da base", restricoes.get("Nível base", "Nível 1"))
        if "Nível 1" in str(nivel) or "N\X\EDvel 1" in str(nivel):
            paredes_terreo.append(w)

    if not paredes_terreo:
        paredes_terreo = ifc.by_type("IfcWall") + ifc.by_type("IfcWallStandardCase")

    # Determinar limites em Y para classificar paredes internas vs externas
    y_posicoes = []
    for w in paredes_terreo:
        if w.ObjectPlacement:
            m = ifcopenshell.util.placement.get_local_placement(w.ObjectPlacement)
            y_posicoes.append(m[1][3] * fator_comp)

    y_min_edif = min(y_posicoes) if y_posicoes else -17.0
    y_max_edif = max(y_posicoes) if y_posicoes else -10.0

    relatorio = []
    linhas_grafico = []

    for w in paredes_terreo:
        comp_m, _, vol_m3 = extrair_dimensoes_elemento(w, "IfcWall", fator_comp)
        if comp_m < 0.2:
            continue

        peso_proprio = vol_m3 * p_alvenaria

        x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
        dir_x, dir_y = 1.0, 0.0
        if w.ObjectPlacement:
            try:
                m = ifcopenshell.util.placement.get_local_placement(w.ObjectPlacement)
                x0 = m[0][3] * fator_comp
                y0 = m[1][3] * fator_comp
                dir_x = m[0][0]
                dir_y = m[1][0]
                norm = (dir_x**2 + dir_y**2)**0.5
                if norm > 0:
                    dir_x /= norm
                    dir_y /= norm
                x1 = x0 + dir_x * comp_m
                y1 = y0 + dir_y * comp_m
            except:
                pass

        # 3. Cálculo da faixa tributária da laje
        # Divisórias curtas (como a do vaso) não suportam laje
        if comp_m < 1.5:
            faixa_tributaria = 0.0
        else:
            # Parede externa (borda) recebe apenas 1 lado (vão / 2)
            # Parede interna recebe ambos os lados (vão)
            eh_borda = (abs(y0 - y_min_edif) < 0.8) or (abs(y0 - y_max_edif) < 0.8)
            if eh_borda:
                faixa_tributaria = vao / 2.0  # ~2.075 m
            else:
                faixa_tributaria = vao        # ~4.15 m

        # Carga linear da laje por pavimento = faixa * pressão
        # Multiplica por 2 caso haja laje de entrepiso + laje de cobertura
        carga_linear_laje = faixa_tributaria * q_superficial_laje
        carga_total_laje = carga_linear_laje * comp_m

        carga_linear_pp = peso_proprio / comp_m if comp_m > 0 else 0
        carga_linear_total = carga_linear_pp + carga_linear_laje
        carga_total_base = peso_proprio + carga_total_laje

        linhas_grafico.append({
            'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
            'carga': carga_linear_total,
            'comprimento': comp_m
        })

        relatorio.append({
            "Nome": w.Name or "Parede",
            "Comprimento (m)": round(comp_m, 2),
            "Faixa Tributária (m)": round(faixa_tributaria, 2),
            "Peso Próprio (kN)": round(peso_proprio, 2),
            "Carga das Lajes (kN)": round(carga_total_laje, 2),
            "CARGA TOTAL NA BASE (kN)": round(carga_total_base, 2),
            "CARGA LINEAR NO RADIER (kN/m)": round(carga_linear_total, 2),
            "Coord X0 (m)": round(x0, 2),
            "Coord Y0 (m)": round(y0, 2),
            "Coord X1 (m)": round(x1, 2),
            "Coord Y1 (m)": round(y1, 2)
        })

    del ifc
    gc.collect()

    # --- PLANTA GRÁFICA ---
    img_buffer = None
    if linhas_grafico:
        x_coords = [p['x0'] for p in linhas_grafico] + [p['x1'] for p in linhas_grafico]
        y_coords = [p['y0'] for p in linhas_grafico] + [p['y1'] for p in linhas_grafico]
        x_min, x_max = min(x_coords) - 1.2, max(x_coords) + 1.2
        y_min, y_max = min(y_coords) - 1.2, max(y_coords) + 1.2

        grid_x = np.arange(x_min, x_max, resolucao)
        grid_y = np.arange(y_min, y_max, resolucao)
        malha = np.zeros((len(grid_y), len(grid_x)), dtype=float)

        for p in linhas_grafico:
            ix0 = int(np.clip(np.searchsorted(grid_x, min(p['x0'], p['x1'])), 0, len(grid_x) - 1))
            ix1 = int(np.clip(np.searchsorted(grid_x, max(p['x0'], p['x1'])), 0, len(grid_x)))
            iy0 = int(np.clip(np.searchsorted(grid_y, min(p['y0'], p['y1'])), 0, len(grid_y) - 1))
            iy1 = int(np.clip(np.searchsorted(grid_y, max(p['y0'], p['y1'])), 0, len(grid_y)))
            if ix0 == ix1: ix1 = min(ix0 + 1, len(grid_x))
            if iy0 == iy1: iy1 = min(iy0 + 1, len(grid_y))
            malha[iy0:iy1, ix0:ix1] += p['carga']

        fig, ax = plt.subplots(figsize=(14, 6))
        cmap = plt.get_cmap('jet')
        img = ax.imshow(malha, extent=[x_min, x_max, y_min, y_max], origin='lower', cmap=cmap, interpolation='gaussian')

        for p in linhas_grafico:
            ax.plot([p['x0'], p['x1']], [p['y0'], p['y1']], color='black', linewidth=1.5, alpha=0.9)
            if rotulos and p['comprimento'] >= 0.7:
                xm = (p['x0'] + p['x1']) / 2.0
                ym = (p['y0'] + p['y1']) / 2.0
                dx = abs(p['x1'] - p['x0'])
                dy = abs(p['y1'] - p['y0'])
                rotacao = 90 if dy > dx else 0
                ax.text(
                    xm, ym, f"{p['carga']:.1f}",
                    fontsize=tam_fonte, fontweight='bold', color='black',
                    ha='center', va='center', rotation=rotacao,
                    bbox=dict(boxstyle='round,pad=0.18', facecolor='white', edgecolor='black', linewidth=0.6, alpha=0.88)
                )

        cbar = plt.colorbar(img, pad=0.02)
        cbar.set_label('Carga Linear Aplicada no Radier (kN/m)', fontsize=11)
        ax.set_title('Planta de Cargas no Radier - Faixas Tributárias Reais (kN/m)', fontsize=14, weight='bold')
        ax.set_xlabel('Coordenada X (m)', fontsize=11)
        ax.set_ylabel('Coordenada Y (m)', fontsize=11)
        ax.grid(color='white', linestyle='--', alpha=0.35)
        fig.tight_layout()

        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format='png', dpi=250)
        plt.close(fig)

    return pd.DataFrame(relatorio), img_buffer

# --- INTERFACE ---
if arquivo_ifc is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
        tmp_file.write(arquivo_ifc.read())
        tmp_caminho = tmp_file.name

    try:
        df, img_buffer = processar_calculo_tributario(
            tmp_caminho, peso_alvenaria, peso_concreto, carga_acidental_laje,
            vao_medio_laje, resolucao_grid, exibir_rotulos, tamanho_fonte
        )

        st.subheader("Resultados Analíticos das Cargas no Radier")
        if not df.empty:
            st.dataframe(df)

            if img_buffer:
                st.image(img_buffer, caption="Planta com Faixas Tributárias Reais e Rótulos Numéricos (kN/m)")

            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Tabela de Cargas')
                if img_buffer:
                    ws = writer.book.create_sheet('Mapa Visual')
                    img_buffer.seek(0)
                    ws.add_image(OpenpyxlImage(img_buffer), 'B2')

            st.download_button(
                label="📥 Descarregar Folha de Cálculo Calibrada (.xlsx)",
                data=excel_buffer.getvalue(),
                file_name="cargas_radier_tributarias.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.error("Não foram encontradas paredes no nível térreo.")

    except Exception as e:
        st.error(f"Erro no processamento: {e}")
    finally:
        os.remove(tmp_caminho)
