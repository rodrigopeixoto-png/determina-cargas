import streamlit as st
import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.placement
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import tempfile
import io
import os

st.set_page_config(page_title="Extrator de Cargas IFC", layout="wide")

st.title("Extração de Cargas para Radier (IFC)")
st.write("Faça o upload de um arquivo IFC para extrair o peso das paredes e visualizar a concentração de cargas no radier.")

# --- BARRA LATERAL ---
st.sidebar.header("Parâmetros de Cálculo")
peso_alvenaria = st.sidebar.number_input("Peso Específico da Parede (kN/m³)", value=13.0, step=1.0)
carga_laje_base = st.sidebar.number_input("Carga da Laje no Radier (kN/m²)", value=5.0, step=1.0)
resolucao_grid = st.sidebar.slider("Resolução do Grid (m)", min_value=0.1, max_value=1.0, value=0.25, step=0.05)

arquivo_ifc = st.file_uploader("Anexe o arquivo .ifc", type=["ifc"])

# Removido o st.cache_data para evitar problemas de consumo de memória no servidor gratuito
def processar_ifc(caminho_temporario, peso_alvenaria):
    ifc = ifcopenshell.open(caminho_temporario)
    paredes = ifc.by_type("IfcWall") + ifc.by_type("IfcWallStandardCase")
    
    dados_relatorio = []
    linhas_paredes = []
    paredes_ignoradas = 0
    
    for parede in paredes:
        try:
            psets = ifcopenshell.util.element.get_psets(parede) or {}
            quantities = psets.get("Qto_WallBaseQuantities", {})
            
            comprimento = quantities.get("Length", 0)
            volume = quantities.get("NetVolume", 0)
            
            if volume == 0:
                dimensions = psets.get("Dimensions", {})
                comprimento = dimensions.get("Length", 0) / 1000
                volume = dimensions.get("Volume", 0)

            # Ignora paredes sem volume ou comprimento para evitar erros matemáticos
            if volume == 0 or comprimento == 0:
                paredes_ignoradas += 1
                continue

            carga_parede_kn = volume * peso_alvenaria
            carga_linear = carga_parede_kn / comprimento
            
            # Verifica se a parede tem posicionamento no espaço antes de tentar ler
            if not parede.ObjectPlacement:
                paredes_ignoradas += 1
                continue
                
            matriz_global = ifcopenshell.util.placement.get_local_placement(parede.ObjectPlacement)
            x_origem = matriz_global[0][3]
            y_origem = matriz_global[1][3]
            
            x_fim = x_origem + comprimento
            y_fim = y_origem 
            
            linhas_paredes.append({
                'x0': x_origem, 'y0': y_origem, 
                'x1': x_fim, 'y1': y_fim, 
                'carga_linear': carga_linear
            })

            dados_relatorio.append({
                "Parede ID": parede.GlobalId,
                "Nome": parede.Name if parede.Name else "Parede Genérica",
                "Comprimento (m)": round(comprimento, 2),
                "Carga Propria (kN)": round(carga_parede_kn, 2),
                "Carga Linear na Base (kN/m)": round(carga_linear, 2),
                "Coord X (m)": round(x_origem, 2),
                "Coord Y (m)": round(y_origem, 2)
            })
        except Exception:
            # Se der erro numa parede específica, contabiliza e passa para a próxima
            paredes_ignoradas += 1
            continue

    return pd.DataFrame(dados_relatorio), linhas_paredes, paredes_ignoradas

def gerar_grafico(linhas_paredes, carga_laje_base, resolucao):
    x_coords = [p['x0'] for p in linhas_paredes] + [p['x1'] for p in linhas_paredes]
    y_coords = [p['y0'] for p in linhas_paredes] + [p['y1'] for p in linhas_paredes]
    
    if not x_coords:
        return None

    x_min, x_max = min(x_coords) - 2, max(x_coords) + 2
    y_min, y_max = min(y_coords) - 2, max(y_coords) + 2

    grid_x = np.arange(x_min, x_max, resolucao)
    grid_y = np.arange(y_min, y_max, resolucao)
    malha_cargas = np.full((len(grid_y), len(grid_x)), float(carga_laje_base))

    for parede in linhas_paredes:
        idx_x0 = np.searchsorted(grid_x, min(parede['x0'], parede['x1']))
        idx_x1 = np.searchsorted(grid_x, max(parede['x0'], parede['x1']))
        idx_y0 = np.searchsorted(grid_y, parede['y0'])
        
        if idx_x0 == idx_x1: idx_x1 += 1
        
        if idx_y0 < len(grid_y):
            malha_cargas[idx_y0, idx_x0:idx_x1] += parede['carga_linear']

    fig, ax = plt.subplots(figsize=(10, 6))
    cmap = plt.get_cmap('jet')
    
    img = ax.imshow(malha_cargas, extent=[x_min, x_max, y_min, y_max], 
                     origin='lower', cmap=cmap, interpolation='gaussian')
    
    for parede in linhas_paredes:
        ax.plot([parede['x0'], parede['x1']], [parede['y0'], parede['y1']], 
                 color='black', linewidth=1.5, alpha=0.7)

    plt.colorbar(img, label='Concentração de Carga (kN/m²)')
    ax.set_title('Mapa de Calor - Cargas no Radier', fontsize=14)
    ax.set_xlabel('Coordenada X (m)')
    ax.set_ylabel('Coordenada Y (m)')
    ax.grid(color='white', linestyle='--', alpha=0.3)
    fig.tight_layout()
    
    return fig

# --- EXECUÇÃO ---
if arquivo_ifc is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
        tmp_file.write(arquivo_ifc.read())
        tmp_caminho = tmp_file.name

    try:
        with st.spinner('Analisando arquivo IFC e extraindo geometria...'):
            df_relatorio, linhas_paredes, paredes_ignoradas = processar_ifc(tmp_caminho, peso_alvenaria)
        
        if paredes_ignoradas > 0:
            st.warning(f"{paredes_ignoradas} elemento(s) ignorado(s) por falta de propriedades geométricas (ex: paredes sem volume ou coordenadas).")

        col1, col2 = st.columns([1, 1])
        
        with col1:
            st.subheader("Relatório Analítico")
            if not df_relatorio.empty:
                st.dataframe(df_relatorio)
                
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df_relatorio.to_excel(writer, index=False, sheet_name='Cargas')
                
                st.download_button("📥 Baixar Tabela Excel", buffer.getvalue(), "cargas_radier.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            else:
                st.error("Nenhuma parede válida foi encontrada no modelo.")

        with col2:
            st.subheader("Mapa Planificado")
            if len(linhas_paredes) > 0:
                fig = gerar_grafico(linhas_paredes, carga_laje_base, resolucao_grid)
                if fig:
                    st.pyplot(fig)
            else:
                st.error("Sem dados geométricos para gerar o gráfico.")

    except Exception as e:
        st.error(f"Erro geral de processamento: {e}")
    
    finally:
        os.remove(tmp_caminho)
