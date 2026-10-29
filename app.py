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

st.title("Extração e Mapa de Cargas para Radier")
st.markdown("""
Faz a **descida de cargas** (Lajes -> Paredes -> Radier) e exporta um **Excel com 2 abas**: 
1. Tabela com os valores finais.
2. Mapa gráfico planificado para auxiliar no lançamento no Eberick/TQS.
""")

st.sidebar.header("Parâmetros de Carga")
peso_alvenaria = st.sidebar.number_input("Peso Específico Parede (kN/m³)", value=13.0, step=1.0)
peso_laje = st.sidebar.number_input("Peso Específico Laje (kN/m³)", value=25.0, step=1.0)
carga_acidental_laje = st.sidebar.number_input("Carga Acidental (kN/m²)", value=2.0, step=0.5)
resolucao_grid = st.sidebar.slider("Resolução do Mapa (m)", 0.1, 1.0, 0.25, 0.05)

arquivo_ifc = st.file_uploader("Anexe o arquivo .ifc", type=["ifc"])

def extrair_geometria_basica(elemento, tipo):
    psets = ifcopenshell.util.element.get_psets(elemento) or {}
    qto_nome = "Qto_WallBaseQuantities" if tipo == "IfcWall" else "Qto_SlabBaseQuantities"
    quantities = psets.get(qto_nome, {})
    
    comprimento = quantities.get("Length", 0)
    volume = quantities.get("NetVolume", 0)
    area = quantities.get("GrossArea", quantities.get("NetArea", 0))
    
    if volume == 0:
        dimensions = psets.get("Dimensions", {})
        comprimento = dimensions.get("Length", 0) / 1000
        volume = dimensions.get("Volume", 0)
        area = dimensions.get("Area", 0)
        
    return comprimento, area, volume

def processar_modelo_e_grafico(caminho_ifc, p_alvenaria, p_laje, q_acidental, resolucao):
    ifc = ifcopenshell.open(caminho_ifc)
    pisos = ifc.by_type("IfcBuildingStorey")
    pisos_ordenados = sorted(pisos, key=lambda p: p.Elevation if p.Elevation is not None else 0, reverse=True)
    
    relatorio_base = []
    linhas_grafico = []
    carga_acumulada_de_cima = 0.0
    
    barra = st.progress(0)
    texto = st.empty()
    
    for idx, piso in enumerate(pisos_ordenados):
        texto.text(f"Processando piso {idx+1}/{len(pisos_ordenados)}: {piso.Name}...")
        
        paredes_piso, lajes_piso = [], []
        for rel in piso.ContainsElements:
            for elem in rel.RelatedElements:
                if elem.is_a("IfcWall") or elem.is_a("IfcWallStandardCase"):
                    paredes_piso.append(elem)
                elif elem.is_a("IfcSlab"):
                    lajes_piso.append(elem)
                    
        carga_total_lajes_piso = 0.0
        for laje in lajes_piso:
            _, area, volume = extrair_geometria_basica(laje, "IfcSlab")
            carga_total_lajes_piso += (volume * p_laje) + (area * q_acidental)
            
        comprimento_total_paredes = 0.0
        dados_paredes = []
        
        for parede in paredes_piso:
            comprimento, _, volume = extrair_geometria_basica(parede, "IfcWall")
            if comprimento > 0 and volume > 0:
                comprimento_total_paredes += comprimento
                dados_paredes.append({
                    "elemento": parede,
                    "comprimento": comprimento,
                    "peso_proprio": volume * p_alvenaria
                })
        
        carga_para_distribuir = carga_total_lajes_piso + carga_acumulada_de_cima
        nova_carga_acumulada = 0.0
        
        eh_ultimo_piso = (idx == len(pisos_ordenados) - 1)
        
        for dp in dados_paredes:
            taxa = dp["comprimento"] / comprimento_total_paredes if comprimento_total_paredes > 0 else 0
            carga_base_parede = dp["peso_proprio"] + (carga_para_distribuir * taxa)
            carga_linear = carga_base_parede / dp["comprimento"]
            nova_carga_acumulada += carga_base_parede
            
            # Se for o radier, guarda os dados para a tabela E extrai as coordenadas para o gráfico
            if eh_ultimo_piso:
                nome = dp["elemento"].Name if dp["elemento"].Name else "Parede"
                relatorio_base.append({
                    "Pavimento": piso.Name,
                    "Nome": nome,
                    "Comprimento (m)": round(dp["comprimento"], 2),
                    "Carga Base (kN)": round(carga_base_parede, 2),
                    "CARGA LINEAR (kN/m)": round(carga_linear, 2)
                })
                
                # Extração de Coordenadas Globais para o Gráfico
                if dp["elemento"].ObjectPlacement:
                    try:
                        matriz = ifcopenshell.util.placement.get_local_placement(dp["elemento"].ObjectPlacement)
                        x0, y0 = matriz[0][3], matriz[1][3]
                        # Vetor de direção X local da parede para saber se está na horizontal ou vertical
                        dir_x, dir_y = matriz[0][0], matriz[1][0]
                        
                        x1 = x0 + dir_x * dp["comprimento"]
                        y1 = y0 + dir_y * dp["comprimento"]
                        
                        linhas_grafico.append({'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1, 'carga': carga_linear})
                    except:
                        pass
                
        carga_acumulada_de_cima = nova_carga_acumulada
        barra.progress((idx + 1) / len(pisos_ordenados))

    # Limpeza CRÍTICA de RAM antes de gerar o gráfico
    del ifc
    gc.collect()
    texto.text("Gerando mapa visual...")

    # --- GERAR O GRÁFICO NA MEMÓRIA ---
    img_buffer = None
    if linhas_grafico:
        x_coords = [p['x0'] for p in linhas_grafico] + [p['x1'] for p in linhas_grafico]
        y_coords = [p['y0'] for p in linhas_grafico] + [p['y1'] for p in linhas_grafico]
        x_min, x_max = min(x_coords) - 1, max(x_coords) + 1
        y_min, y_max = min(y_coords) - 1, max(y_coords) + 1

        grid_x = np.arange(x_min, x_max, resolucao)
        grid_y = np.arange(y_min, y_max, resolucao)
        malha = np.zeros((len(grid_y), len(grid_x)))

        for p in linhas_grafico:
            ix0 = np.searchsorted(grid_x, min(p['x0'], p['x1']))
            ix1 = np.searchsorted(grid_x, max(p['x0'], p['x1']))
            iy0 = np.searchsorted(grid_y, min(p['y0'], p['y1']))
            iy1 = np.searchsorted(grid_y, max(p['y0'], p['y1']))
            
            if ix0 == ix1: ix1 += 1
            if iy0 == iy1: iy1 += 1
            
            if iy1 <= len(grid_y) and ix1 <= len(grid_x):
                malha[iy0:iy1, ix0:ix1] += p['carga']

        fig, ax = plt.subplots(figsize=(10, 6))
        cmap = plt.get_cmap('jet')
        img = ax.imshow(malha, extent=[x_min, x_max, y_min, y_max], origin='lower', cmap=cmap)
        
        for p in linhas_grafico:
            ax.plot([p['x0'], p['x1']], [p['y0'], p['y1']], color='black', linewidth=1.5, alpha=0.8)

        plt.colorbar(img, label='Carga Linear (kN/m)')
        ax.set_title('Mapa de Concentração no Radier')
        ax.set_xlabel('X (m)')
        ax.set_ylabel('Y (m)')
        fig.tight_layout()
        
        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format='png', dpi=150)
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
        df, img_buffer = processar_modelo_e_grafico(tmp_caminho, peso_alvenaria, peso_laje, carga_acidental_laje, resolucao_grid)
        
        st.subheader("Resultados no Radier")
        if not df.empty:
            st.dataframe(df, use_container_width=True)
            
            if img_buffer:
                st.image(img_buffer, caption="Este mapa será exportado no Excel", use_container_width=True)
            
            # --- CRIAR EXCEL COM 2 ABAS ---
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
                label="📥 Baixar Excel Completo (Tabela + Mapa)", 
                data=excel_buffer.getvalue(), 
                file_name="cargas_eberick.xlsx", 
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.error("Nenhuma carga encontrada no piso térreo.")

    except Exception as e:
        st.error(f"Erro no processamento: {e}")
    finally:
        os.remove(tmp_caminho)
