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

st.title("Extração de Cargas para Radier (IFC)")
st.write("Versão Otimizada: Processamento focado em baixo consumo de memória.")

st.sidebar.header("Parâmetros")
peso_alvenaria = st.sidebar.number_input("Peso Específico da Parede (kN/m³)", value=13.0, step=1.0)

arquivo_ifc = st.file_uploader("Anexe o arquivo .ifc", type=["ifc"])

def processar_ifc_otimizado(caminho_temporario, peso_alvenaria):
    # Abre o ficheiro sem carregar representações 3D pesadas
    ifc = ifcopenshell.open(caminho_temporario)
    
    # Extrai apenas os IDs para não manter todos os objetos em memória
    paredes_ids = [p.id() for p in ifc.by_type("IfcWall")] + [p.id() for p in ifc.by_type("IfcWallStandardCase")]
    
    dados_relatorio = []
    
    # Processador de progresso para a interface
    barra_progresso = st.progress(0)
    texto_progresso = st.empty()
    total = len(paredes_ids)
    
    for i, p_id in enumerate(paredes_ids):
        try:
            parede = ifc.by_id(p_id)
            psets = ifcopenshell.util.element.get_psets(parede) or {}
            quantities = psets.get("Qto_WallBaseQuantities", {})
            
            comprimento = quantities.get("Length", 0)
            volume = quantities.get("NetVolume", 0)
            
            if volume == 0:
                dimensions = psets.get("Dimensions", {})
                comprimento = dimensions.get("Length", 0) / 1000
                volume = dimensions.get("Volume", 0)

            if volume == 0 or comprimento == 0:
                continue

            carga_parede = volume * peso_alvenaria
            carga_linear = carga_parede / comprimento
            
            x_origem, y_origem = 0.0, 0.0
            if parede.ObjectPlacement:
                matriz = ifcopenshell.util.placement.get_local_placement(parede.ObjectPlacement)
                x_origem = matriz[0][3]
                y_origem = matriz[1][3]

            dados_relatorio.append({
                "Nome": parede.Name if parede.Name else "Parede",
                "Compr (m)": round(comprimento, 2),
                "Carga (kN)": round(carga_parede, 2),
                "Linear (kN/m)": round(carga_linear, 2),
                "X (m)": round(x_origem, 2),
                "Y (m)": round(y_origem, 2)
            })
            
        except Exception:
            pass
            
        # Atualiza o progresso
        if i % 10 == 0:
            barra_progresso.progress((i + 1) / total)
            texto_progresso.text(f"A processar: {i+1} de {total} paredes...")
            
    # Força a limpeza da memória RAM
    del ifc
    gc.collect()
    
    barra_progresso.empty()
    texto_progresso.empty()
    
    return pd.DataFrame(dados_relatorio)

if arquivo_ifc is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
        tmp_file.write(arquivo_ifc.read())
        tmp_caminho = tmp_file.name

    try:
        df_relatorio = processar_ifc_otimizado(tmp_caminho, peso_alvenaria)
        
        st.subheader("Relatório de Cargas")
        if not df_relatorio.empty:
            st.dataframe(df_relatorio)
            
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                df_relatorio.to_excel(writer, index=False, sheet_name='Cargas')
            
            st.download_button("📥 Baixar Tabela Excel", buffer.getvalue(), "cargas.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            st.warning("Não foram encontradas paredes com dados de volume válidos.")

    except Exception as e:
        st.error(f"Erro de memória: {e}")
    finally:
        os.remove(tmp_caminho)
