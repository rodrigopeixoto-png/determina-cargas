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
Esta rotina realiza a **descida de cargas** (Lajes + Paredes superiores -> Paredes do piso térreo) 
e gera o **mapa planificado de cargas lineares (kN/m)** para aplicação direta no radier.
""")

# --- PARÂMETROS ---
st.sidebar.header("Parâmetros de Carga")
peso_alvenaria = st.sidebar.number_input("Peso Específico da Alvenaria/Parede (kN/m³)", value=13.0, step=1.0)
peso_concreto = st.sidebar.number_input("Peso Específico do Concreto da Laje (kN/m³)", value=25.0, step=1.0)
carga_acidental_laje = st.sidebar.number_input("Sobrecarga Acidental/Revestimento na Laje (kN/m²)", value=2.0, step=0.5)
resolucao_grid = st.sidebar.slider("Resolução da Malha Gráfica (m)", 0.1, 0.5, 0.25, 0.05)

arquivo_ifc = st.file_uploader("Anexe o ficheiro IFC estrutural ou arquitetónico", type=["ifc"])

def obter_fator_comprimento(ifc):
    """Identifica se o IFC está em milímetros, centímetros ou metros"""
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

def extrair_dimensoes_elemento(elemento, tipo, fator_comprimento):
    """Extrai dimensões geométricas convertendo comprimentos para metros"""
    psets = ifcopenshell.util.element.get_psets(elemento) or {}
    
    qto_nome = "Qto_WallBaseQuantities" if tipo == "IfcWall" else "Qto_SlabBaseQuantities"
    quantities = psets.get(qto_nome, {})
    
    comprimento = quantities.get("Length", 0.0)
    volume = quantities.get("NetVolume", quantities.get("GrossVolume", 0.0))
    area = quantities.get("NetArea", quantities.get("GrossArea", 0.0))
    
    if volume == 0.0 or comprimento == 0.0:
        cotas = psets.get("Cotas", {})
        if comprimento == 0.0:
            comprimento = cotas.get("Comprimento", 0.0)
        if volume == 0.0:
            volume = cotas.get("Volume", 0.0)
        if area == 0.0:
            area = cotas.get("Área", 0.0)
            
    # Converte comprimentos de milímetros para metros se necessário
    if comprimento > 100.0:
        comprimento = comprimento * fator_comprimento
        
    return float(comprimento), float(area), float(volume)

def processar_descida_e_grafico(caminho_ifc, p_alvenaria, p_concreto, q_acidental, resolucao):
    ifc = ifcopenshell.open(caminho_ifc)
    fator_comp = obter_fator_comprimento(ifc)
    
    pisos = ifc.by_type("IfcBuildingStorey")
    pisos_ordenados = sorted(pisos, key=lambda p: p.Elevation if p.Elevation is not None else 0.0, reverse=True)
    
    relatorio_base = []
    linhas_grafico = []
    carga_acumulada_superior = 0.0
    
    barra = st.progress(0)
    texto = st.empty()
    
    for idx, piso in enumerate(pisos_ordenados):
        texto.text(f"A calcular descida de cargas: {piso.Name}...")
        
        paredes_piso, lajes_piso = [], []
        if piso.ContainsElements:
            for rel in piso.ContainsElements:
                for elem in rel.RelatedElements:
                    if elem.is_a("IfcWall") or elem.is_a("IfcWallStandardCase"):
                        paredes_piso.append(elem)
                    elif elem.is_a("IfcSlab"):
                        lajes_piso.append(elem)
                        
        # Fallback de associação caso o Revit não tenha estruturado a contenção espacial
        if not paredes_piso and idx == len(pisos_ordenados) - 1:
            paredes_piso = ifc.by_type("IfcWall") + ifc.by_type("IfcWallStandardCase")
        if not lajes_piso and idx < len(pisos_ordenados) - 1:
            lajes_piso = ifc.by_type("IfcSlab")

        # 1. Carga total das lajes do piso
        carga_total_lajes = 0.0
        for laje in lajes_piso:
            _, area_m2, vol_m3 = extrair_dimensoes_elemento(laje, "IfcSlab", fator_comp)
            carga_total_lajes += (vol_m3 * p_concreto) + (area_m2 * q_acidental)

        # 2. Dimensões das paredes deste piso
        comp_total_paredes = 0.0
        dados_paredes = []
        for parede in paredes_piso:
            comp_m, _, vol_m3 = extrair_dimensoes_elemento(parede, "IfcWall", fator_comp)
            if comp_m > 0.1 and vol_m3 > 0.0:
                comp_total_paredes += comp_m
                dados_paredes.append({
                    "elemento": parede,
                    "comprimento_m": comp_m,
                    "peso_proprio": vol_m3 * p_alvenaria
                })

        carga_distribuir = carga_total_lajes + carga_acumulada_superior
        nova_carga_acumulada = 0.0
        eh_radier = (idx == len(pisos_ordenados) - 1)
        
        for dp in dados_paredes:
            taxa = dp["comprimento_m"] / comp_total_paredes if comp_total_paredes > 0 else 0
            carga_laje_absorvida = carga_distribuir * taxa
            carga_total_base = dp["peso_proprio"] + carga_laje_absorvida
            carga_linear = carga_total_base / dp["comprimento_m"]
            nova_carga_acumulada += carga_total_base
            
            if eh_radier:
                nome_parede = dp["elemento"].Name if dp["elemento"].Name else "Parede"
                
                # Extração de Coordenadas Globais em METROS
                x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
                if dp["elemento"].ObjectPlacement:
                    try:
                        matriz = ifcopenshell.util.placement.get_local_placement(dp["elemento"].ObjectPlacement)
                        # Multiplica por fator_comp para garantir coordenadas em metros
                        x0 = matriz[0][3] * fator_comp
                        y0 = matriz[1][3] * fator_comp
                        
                        dir_x = matriz[0][0]
                        dir_y = matriz[1][0]
                        norm = (dir_x**2 + dir_y**2)**0.5
                        if norm > 0:
                            dir_x /= norm
                            dir_y /= norm
                            
                        x1 = x0 + dir_x * dp["comprimento_m"]
                        y1 = y0 + dir_y * dp["comprimento_m"]
                        
                        linhas_grafico.append({
                            'x0': x0, 'y0': y0, 
                            'x1': x1, 'y1': y1, 
                            'carga': carga_linear
                        })
                    except Exception:
                        pass

                relatorio_base.append({
                    "Nome": nome_parede,
                    "Comprimento (m)": round(dp["comprimento_m"], 2),
                    "Peso Próprio (kN)": round(dp["peso_proprio"], 2),
                    "Carga das Lajes (kN)": round(carga_laje_absorvida, 2),
                    "CARGA TOTAL NA BASE (kN)": round(carga_total_base, 2),
                    "CARGA LINEAR (kN/m)": round(carga_linear, 2),
                    "Coord X0 (m)": round(x0, 2),
                    "Coord Y0 (m)": round(y0, 2),
                    "Coord X1 (m)": round(x1, 2),
                    "Coord Y1 (m)": round(y1, 2)
                })

        carga_acumulada_superior = nova_carga_acumulada
        barra.progress((idx + 1) / len(pisos_ordenados))

    del ifc
    gc.collect()
    texto.text("A gerar a representação gráfica do radier...")

    # --- GERAÇÃO DO MAPA VISUAL ---
    img_buffer = None
    if linhas_grafico:
        x_coords = [p['x0'] for p in linhas_grafico] + [p['x1'] for p in linhas_grafico]
        y_coords = [p['y0'] for p in linhas_grafico] + [p['y1'] for p in linhas_grafico]
        
        x_min, x_max = min(x_coords) - 1.0, max(x_coords) + 1.0
        y_min, y_max = min(y_coords) - 1.0, max(y_coords) + 1.0

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

        fig, ax = plt.subplots(figsize=(11, 5))
        cmap = plt.get_cmap('jet')
        img = ax.imshow(malha, extent=[x_min, x_max, y_min, y_max], origin='lower', cmap=cmap, interpolation='gaussian')
        
        for p in linhas_grafico:
            ax.plot([p['x0'], p['x1']], [p['y0'], p['y1']], color='black', linewidth=1.5, alpha=0.85)

        cbar = plt.colorbar(img)
        cbar.set_label('Carga Linear Aplicada (kN/m)', fontsize=10)
        ax.set_title('Planta de Concentração de Cargas no Radier (kN/m)', fontsize=13, weight='bold')
        ax.set_xlabel('Coordenada X (m)')
        ax.set_ylabel('Coordenada Y (m)')
        ax.grid(color='white', linestyle='--', alpha=0.3)
        fig.tight_layout()
        
        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format='png', dpi=200)
        plt.close(fig)

    texto.empty()
    barra.empty()
    return pd.DataFrame(relatorio_base), img_buffer

# --- EXECUÇÃO PRINCIPAL ---
if arquivo_ifc is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
        tmp_file.write(arquivo_ifc.read())
        tmp_caminho = tmp_file.name

    try:
        df, img_buffer = processar_descida_e_grafico(
            tmp_caminho, peso_alvenaria, peso_concreto, carga_acidental_laje, resolucao_grid
        )
        
        st.subheader("Resultados Analíticos das Cargas no Radier")
        if not df.empty:
            st.dataframe(df)
            
            if img_buffer:
                st.image(img_buffer, caption="Mapa de Cargas Planificado (kN/m) para Lançamento no Eberick")
            
            # Exportação de Excel com 2 Abas (Tabela + Desenho)
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Tabela de Cargas')
                
                if img_buffer:
                    workbook = writer.book
                    worksheet = workbook.create_sheet('Mapa Visual')
                    img_buffer.seek(0)
                    xl_img = OpenpyxlImage(img_buffer)
                    worksheet.add_image(xl_img, 'B2')
            
            st.download_button(
                label="📥 Descarregar Folha de Cálculo Completa (.xlsx)", 
                data=excel_buffer.getvalue(), 
                file_name="descida_cargas_radier_eberick.xlsx", 
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.error("Não foram encontradas paredes válidas no nível térreo.")

    except Exception as e:
        st.error(f"Erro durante o cálculo: {e}")
    finally:
        os.remove(tmp_caminho)
