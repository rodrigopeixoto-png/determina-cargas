import streamlit as st
import ifcopenshell
import ifcopenshell.util.element
import pandas as pd
import tempfile
import io
import os
import gc

st.set_page_config(page_title="Descida de Cargas IFC", layout="wide")

st.title("Descida de Cargas para Radier (Paredes + Lajes)")
st.markdown("""
Esta rotina calcula a descida de cargas de cima para baixo. 
A carga das lajes de cada piso é distribuída pelas paredes desse mesmo piso (proporcionalmente ao comprimento de cada parede). 
A carga acumulada desce para os pisos inferiores até chegar à base (Radier).
""")

st.sidebar.header("Parâmetros de Carga")
peso_alvenaria = st.sidebar.number_input("Peso Específico da Parede (kN/m³)", value=13.0, step=1.0)
peso_laje = st.sidebar.number_input("Peso Específico da Laje (kN/m³)", value=25.0, step=1.0)
carga_acidental_laje = st.sidebar.number_input("Carga Acidental/Revestimento na Laje (kN/m²)", value=2.0, step=0.5)

arquivo_ifc = st.file_uploader("Anexe o arquivo .ifc", type=["ifc"])

def extrair_geometria_basica(elemento, tipo):
    """Tenta extrair as dimensões básicas sem carregar a malha 3D pesada"""
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

def processar_descida_cargas(caminho_ifc, p_alvenaria, p_laje, q_acidental):
    ifc = ifcopenshell.open(caminho_ifc)
    
    # 1. Agrupar elementos por Pavimento (IfcBuildingStorey)
    pisos = ifc.by_type("IfcBuildingStorey")
    # Ordenar pisos pela elevação (Z) de cima para baixo (descida de cargas)
    pisos_ordenados = sorted(pisos, key=lambda p: p.Elevation if p.Elevation is not None else 0, reverse=True)
    
    relatorio_base = []
    carga_acumulada_de_cima = 0.0  # Carga que vem dos andares superiores
    
    barra = st.progress(0)
    texto = st.empty()
    
    for idx, piso in enumerate(pisos_ordenados):
        texto.text(f"A calcular descida de cargas: {piso.Name}...")
        
        # Obter elementos contidos neste piso
        paredes_piso = []
        lajes_piso = []
        
        # Relacionamento de contenção espacial
        for rel in piso.ContainsElements:
            for elem in rel.RelatedElements:
                if elem.is_a("IfcWall") or elem.is_a("IfcWallStandardCase"):
                    paredes_piso.append(elem)
                elif elem.is_a("IfcSlab"):
                    lajes_piso.append(elem)
                    
        # 2. Calcular o peso total das lajes neste piso
        carga_total_lajes_piso = 0.0
        for laje in lajes_piso:
            _, area, volume = extrair_geometria_basica(laje, "IfcSlab")
            peso_proprio = volume * p_laje
            carga_acidental_total = area * q_acidental
            carga_total_lajes_piso += (peso_proprio + carga_acidental_total)
            
        # 3. Distribuir as cargas para as paredes deste piso
        comprimento_total_paredes = 0.0
        dados_paredes_atuais = []
        
        for parede in paredes_piso:
            comprimento, _, volume = extrair_geometria_basica(parede, "IfcWall")
            if comprimento > 0 and volume > 0:
                comprimento_total_paredes += comprimento
                peso_proprio_parede = volume * p_alvenaria
                dados_paredes_atuais.append({
                    "id": parede.id(),
                    "guid": parede.GlobalId,
                    "nome": parede.Name if parede.Name else "Parede",
                    "comprimento": comprimento,
                    "peso_proprio": peso_proprio_parede
                })
        
        # A carga a distribuir pelas paredes = Carga da Laje do Piso + Cargas que vêm dos pisos de cima
        carga_para_distribuir = carga_total_lajes_piso + carga_acumulada_de_cima
        nova_carga_acumulada_para_baixo = 0.0
        
        # 4. Aplicar a distribuição proporcional baseada no comprimento
        for dp in dados_paredes_atuais:
            taxa_distribuicao = dp["comprimento"] / comprimento_total_paredes if comprimento_total_paredes > 0 else 0
            
            # Carga da laje que esta parede absorve
            carga_laje_absorvida = carga_para_distribuir * taxa_distribuicao
            
            # Carga final na base desta parede
            carga_total_base_parede = dp["peso_proprio"] + carga_laje_absorvida
            carga_linear = carga_total_base_parede / dp["comprimento"]
            
            # Somar para o piso de baixo
            nova_carga_acumulada_para_baixo += carga_total_base_parede
            
            # Se for o piso mais baixo (Térreo/Radier), guardamos para o relatório final
            if idx == len(pisos_ordenados) - 1:
                relatorio_base.append({
                    "Pavimento": piso.Name,
                    "Nome Parede": dp["nome"],
                    "Comprimento (m)": round(dp["comprimento"], 2),
                    "Peso Próprio (kN)": round(dp["peso_proprio"], 2),
                    "Carga das Lajes/Superiores (kN)": round(carga_laje_absorvida, 2),
                    "CARGA TOTAL NA BASE (kN)": round(carga_total_base_parede, 2),
                    "CARGA LINEAR NO RADIER (kN/m)": round(carga_linear, 2)
                })
                
        # Atualizar a carga que desce para o próximo ciclo (piso de baixo)
        carga_acumulada_de_cima = nova_carga_acumulada_para_baixo
        barra.progress((idx + 1) / len(pisos_ordenados))

    del ifc
    gc.collect()
    texto.empty()
    barra.empty()
    
    return pd.DataFrame(relatorio_base)


if arquivo_ifc is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
        tmp_file.write(arquivo_ifc.read())
        tmp_caminho = tmp_file.name

    try:
        df_resultado = processar_descida_cargas(tmp_caminho, peso_alvenaria, peso_laje, carga_acidental_laje)
        
        st.subheader("Cargas Finais no Nível do Radier")
        if not df_resultado.empty:
            st.dataframe(df_resultado, use_container_width=True)
            
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                df_resultado.to_excel(writer, index=False, sheet_name='Cargas no Radier')
            
            st.download_button(
                label="📥 Baixar Planilha para Radier (Excel)", 
                data=buffer.getvalue(), 
                file_name="descida_cargas_radier.xlsx", 
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            
            st.success("Cálculo concluído! As cargas das lajes foram distribuídas e acumuladas com o peso das paredes com sucesso.")
        else:
            st.error("Não foram encontradas paredes no nível inferior ou o modelo não está estruturado por pavimentos (IfcBuildingStorey).")

    except Exception as e:
        st.error(f"Erro ao processar: {e}")
    finally:
        os.remove(tmp_caminho)
