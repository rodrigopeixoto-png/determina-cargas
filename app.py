import streamlit as st
import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.placement
import pandas as pd
import tempfile
import io
import os
import gc

st.set_page_config(page_title="Extrator de Cargas IFC", layout="wide")

st.title("Extração de Cargas (Paredes e Lajes)")
st.write("Versão Otimizada: Extrai paredes e lajes em abas separadas.")

st.sidebar.header("Parâmetros")
peso_alvenaria = st.sidebar.number_input("Peso Específico da Parede (kN/m³)", value=13.0, step=1.0)
peso_concreto = st.sidebar.number_input("Peso Específico da Laje (kN/m³)", value=25.0, step=1.0)

arquivo_ifc = st.file_uploader("Anexe o arquivo .ifc", type=["ifc"])

def extrair_dados_elemento(ifc, element_ids, peso_especifico, tipo="Parede"):
    dados = []
    
    for p_id in element_ids:
        try:
            elemento = ifc.by_id(p_id)
            psets = ifcopenshell.util.element.get_psets(elemento) or {}
            
            # Tenta buscar nas quantidades base (parede ou laje)
            qto_nome = "Qto_WallBaseQuantities" if tipo == "Parede" else "Qto_SlabBaseQuantities"
            quantities = psets.get(qto_nome, {})
            
            comprimento = quantities.get("Length", 0) # Para lajes pode não ter comprimento linear direto
            volume = quantities.get("NetVolume", 0)
            area = quantities.get("GrossArea", quantities.get("NetArea", 0))
            
            if volume == 0:
                dimensions = psets.get("Dimensions", {})
                comprimento = dimensions.get("Length", 0) / 1000
                volume = dimensions.get("Volume", 0)
                area = dimensions.get("Area", 0)

            if volume == 0:
                continue

            carga_total = volume * peso_especifico
            
            x_origem, y_origem = 0.0, 0.0
            if elemento.ObjectPlacement:
                matriz = ifcopenshell.util.placement.get_local_placement(elemento.ObjectPlacement)
                x_origem = matriz[0][3]
                y_origem = matriz[1][3]

            linha_dado = {
                "Nome": elemento.Name if elemento.Name else f"{tipo} Genérica",
                "Volume (m³)": round(volume, 2),
                "Carga Total (kN)": round(carga_total, 2),
                "X (m)": round(x_origem, 2),
                "Y (m)": round(y_origem, 2)
            }
            
            if tipo == "Parede" and comprimento > 0:
                linha_dado["Compr (m)"] = round(comprimento, 2)
                linha_dado["Linear (kN/m)"] = round(carga_total / comprimento, 2)
            elif tipo == "Laje" and area > 0:
                linha_dado["Área (m²)"] = round(area, 2)
                linha_dado["Superficial (kN/m²)"] = round(carga_total / area, 2)
                
            dados.append(linha_dado)
            
        except Exception:
            pass
            
    return dados

def processar_ifc_completo(caminho_temporario, peso_alvenaria, peso_concreto):
    ifc = ifcopenshell.open(caminho_temporario)
    
    paredes_ids = [p.id() for p in ifc.by_type("IfcWall")] + [p.id() for p in ifc.by_type("IfcWallStandardCase")]
    lajes_ids = [p.id() for p in ifc.by_type("IfcSlab")]
    
    st.write(f"Encontradas {len(paredes_ids)} paredes e {len(lajes_ids)} lajes. A processar...")
    
    dados_paredes = extrair_dados_elemento(ifc, paredes_ids, peso_alvenaria, "Parede")
    dados_lajes = extrair_dados_elemento(ifc, lajes_ids, peso_concreto, "Laje")
            
    del ifc
    gc.collect()
    
    return pd.DataFrame(dados_paredes), pd.DataFrame(dados_lajes)

if arquivo_ifc is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
        tmp_file.write(arquivo_ifc.read())
        tmp_caminho = tmp_file.name

    try:
        df_paredes, df_lajes = processar_ifc_completo(tmp_caminho, peso_alvenaria, peso_concreto)
        
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Relatório de Paredes")
            st.dataframe(df_paredes)
        with col2:
            st.subheader("Relatório de Lajes")
            st.dataframe(df_lajes)
            
        if not df_paredes.empty or not df_lajes.empty:
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                if not df_paredes.empty:
                    df_paredes.to_excel(writer, index=False, sheet_name='Paredes')
                if not df_lajes.empty:
                    df_lajes.to_excel(writer, index=False, sheet_name='Lajes')
            
            st.download_button("📥 Baixar Tabela Excel (Paredes e Lajes)", buffer.getvalue(), "cargas_completas.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    except Exception as e:
        st.error(f"Erro de processamento: {e}")
    finally:
        os.remove(tmp_caminho)
