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

try:
    import plotly.graph_objects as go
    TEM_PLOTLY = True
except ImportError:
    TEM_PLOTLY = False

st.set_page_config(page_title="Cargas no Radier - IFC", layout="wide")

st.title("Descida de Cargas para Radier (IFC -> Eberick)")
st.markdown("""
Esta plataforma calcula o peso próprio e a transferência tributária das lajes sobre as paredes, gerando a **tabela detalhada por componente**, a **planta cotada 2D** e o **modelo tridimensional interativo** para lançamento estrutural.
""")

# --- BARRA LATERAL ---
st.sidebar.header("Parâmetros de Carga")
peso_alvenaria = st.sidebar.number_input("Peso Específico da Parede (kN/m³)", value=25.0, step=1.0)
peso_concreto = st.sidebar.number_input("Peso Específico da Laje (kN/m³)", value=25.0, step=1.0)
carga_acidental_laje = st.sidebar.number_input("Sobrecarga Acidental na Laje (kN/m²)", value=2.0, step=0.5)

st.sidebar.header("Geometria Tributária")
vao_medio_laje = st.sidebar.number_input("Vão Médio dos Compartimentos (m)", value=4.15, step=0.05)

st.sidebar.header("Visualização 2D")
exibir_rotulos_2d = st.sidebar.checkbox("Exibir rótulos numéricos no mapa 2D", value=True)
comp_minimo_rotulo = st.sidebar.slider("Comprimento mínimo para rotular no 2D (m)", 0.5, 3.0, 1.5, 0.1)
dist_min_entre_rotulos = st.sidebar.slider("Distância mínima entre textos 2D (m)", 0.5, 2.5, 1.2, 0.1)
tamanho_fonte = st.sidebar.slider("Tamanho da fonte 2D", 5, 11, 7)
resolucao_grid = st.sidebar.slider("Resolução da Malha 2D (m)", 0.1, 0.5, 0.25, 0.05)

arquivo_ifc = st.file_uploader("Anexe o ficheiro IFC", type=["ifc"])

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

def processar_calculo_completo(caminho_ifc, p_alvenaria, p_concreto, q_acidental, vao, 
                               resolucao, rotulos, comp_min_rot, dist_min_rot, tam_fonte):
    ifc = ifcopenshell.open(caminho_ifc)
    fator_comp = obter_fator_comprimento(ifc)
    
    lajes_elevadas = []
    for s in ifc.by_type("IfcSlab"):
        nome = (s.Name or "").upper()
        if "LAJE_PISO_20CM" in nome or "PISO_20CM" in nome or "RADIER" in nome:
            continue
        lajes_elevadas.append(s)

    if lajes_elevadas:
        _, a_tot, v_tot = extrair_dimensoes_elemento(lajes_elevadas[0], "IfcSlab", fator_comp)
        espessura_media = (v_tot / a_tot) if a_tot > 0 else 0.15
    else:
        espessura_media = 0.15

    q_superficial_laje = (espessura_media * p_concreto) + q_acidental

    paredes_terreo = []
    for w in ifc.by_type("IfcWall") + ifc.by_type("IfcWallStandardCase"):
        psets = ifcopenshell.util.element.get_psets(w) or {}
        restricoes = psets.get("Restrições", {})
        nivel = restricoes.get("Restrição da base", restricoes.get("Nível base", "Nível 1"))
        if "Nível 1" in str(nivel) or "N\X\EDvel 1" in str(nivel):
            paredes_terreo.append(w)

    if not paredes_terreo:
        paredes_terreo = ifc.by_type("IfcWall") + ifc.by_type("IfcWallStandardCase")

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

        peso_proprio_total = vol_m3 * p_alvenaria
        peso_proprio_linear = peso_proprio_total / comp_m if comp_m > 0 else 0.0

        x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
        if w.ObjectPlacement:
            try:
                m = ifcopenshell.util.placement.get_local_placement(w.ObjectPlacement)
                x0 = m[0][3] * fator_comp
                y0 = m[1][3] * fator_comp
                dir_x, dir_y = m[0][0], m[1][0]
                norm = (dir_x**2 + dir_y**2)**0.5
                if norm > 0:
                    dir_x /= norm
                    dir_y /= norm
                x1 = x0 + dir_x * comp_m
                y1 = y0 + dir_y * comp_m
            except:
                pass

        # Classificação da influência da laje
        if comp_m < 1.5:
            condicao_apoio = "Divisória Interna (Sem Carga de Laje)"
            largura_influencia = 0.0
        else:
            eh_borda = (abs(y0 - y_min_edif) < 0.8) or (abs(y0 - y_max_edif) < 0.8)
            if eh_borda:
                condicao_apoio = "Apoio Simples (Fachada / Borda)"
                largura_influencia = vao / 2.0
            else:
                condicao_apoio = "Apoio Duplo (Meação entre Quartos)"
                largura_influencia = vao

        carga_linear_laje = largura_influencia * q_superficial_laje
        carga_total_laje = carga_linear_laje * comp_m

        carga_linear_total = peso_proprio_linear + carga_linear_laje
        carga_total_base = peso_proprio_total + carga_total_laje

        linhas_grafico.append({
            'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
            'carga': carga_linear_total,
            'carga_laje': carga_linear_laje,
            'carga_pp': peso_proprio_linear,
            'comprimento': comp_m,
            'nome': w.Name or "Parede"
        })

        relatorio.append({
            "Identificação da Parede": w.Name or f"Parede #{w.id()}",
            "Comprimento (m)": round(comp_m, 2),
            "Condição de Apoio da Laje": condicao_apoio,
            "Largura de Influência da Laje (m)": round(largura_influencia, 2),
            "Pressão da Laje (kN/m²)": round(q_superficial_laje, 2),
            "Carga Linear da Laje (kN/m)": round(carga_linear_laje, 2),
            "Carga Total da Laje (kN)": round(carga_total_laje, 2),
            "Carga Linear Peso Próprio (kN/m)": round(peso_proprio_linear, 2),
            "Peso Próprio Total (kN)": round(peso_proprio_total, 2),
            "CARGA TOTAL NA BASE (kN)": round(carga_total_base, 2),
            "CARGA LINEAR NO RADIER (kN/m)": round(carga_linear_total, 2),
            "Coord X0 (m)": round(x0, 2),
            "Coord Y0 (m)": round(y0, 2),
            "Coord X1 (m)": round(x1, 2),
            "Coord Y1 (m)": round(y1, 2)
        })

    del ifc
    gc.collect()

    # --- FUSÃO DE ELEMENTOS COINCIDENTES PARA A PLANTA 2D ---
    linhas_consolidadas = []
    for p in linhas_grafico:
        xm, ym = (p['x0'] + p['x1']) / 2.0, (p['y0'] + p['y1']) / 2.0
        encontrado = False
        for c in linhas_consolidadas:
            cxm, cym = (c['x0'] + c['x1']) / 2.0, (c['y0'] + c['y1']) / 2.0
            if ((xm - cxm)**2 + (ym - cym)**2)**0.5 < 0.35:
                if p['carga'] > c['carga']:
                    c.update(p)
                encontrado = True
                break
        if not encontrado:
            linhas_consolidadas.append(dict(p))

    # --- MAPA 2D ---
    img_buffer = None
    if linhas_consolidadas:
        x_coords = [p['x0'] for p in linhas_consolidadas] + [p['x1'] for p in linhas_consolidadas]
        y_coords = [p['y0'] for p in linhas_consolidadas] + [p['y1'] for p in linhas_consolidadas]
        x_min, x_max = min(x_coords) - 1.2, max(x_coords) + 1.2
        y_min, y_max = min(y_coords) - 1.2, max(y_coords) + 1.2

        grid_x = np.arange(x_min, x_max, resolucao)
        grid_y = np.arange(y_min, y_max, resolucao)
        malha = np.zeros((len(grid_y), len(grid_x)), dtype=float)

        for p in linhas_consolidadas:
            ix0 = int(np.clip(np.searchsorted(grid_x, min(p['x0'], p['x1'])), 0, len(grid_x) - 1))
            ix1 = int(np.clip(np.searchsorted(grid_x, max(p['x0'], p['x1'])), 0, len(grid_x)))
            iy0 = int(np.clip(np.searchsorted(grid_y, min(p['y0'], p['y1'])), 0, len(grid_y) - 1))
            iy1 = int(np.clip(np.searchsorted(grid_y, max(p['y0'], p['y1'])), 0, len(grid_y)))
            if ix0 == ix1: ix1 = min(ix0 + 1, len(grid_x))
            if iy0 == iy1: iy1 = min(iy0 + 1, len(grid_y))
            malha[iy0:iy1, ix0:ix1] += p['carga']

        fig, ax = plt.subplots(figsize=(15, 6))
        cmap = plt.get_cmap('jet')
        img = ax.imshow(malha, extent=[x_min, x_max, y_min, y_max], origin='lower', cmap=cmap, interpolation='gaussian')

        for p in linhas_consolidadas:
            ax.plot([p['x0'], p['x1']], [p['y0'], p['y1']], color='black', linewidth=1.5, alpha=0.9)

        linhas_ordenadas = sorted(linhas_consolidadas, key=lambda x: x['comprimento'], reverse=True)
        rotulos_colocados = []

        if rotulos:
            for p in linhas_ordenadas:
                if p['comprimento'] < comp_min_rot:
                    continue

                xm = (p['x0'] + p['x1']) / 2.0
                ym = (p['y0'] + p['y1']) / 2.0

                colide = False
                for rx, ry in rotulos_colocados:
                    if ((xm - rx)**2 + (ym - ry)**2)**0.5 < dist_min_rot:
                        colide = True
                        break

                if colide:
                    continue

                rotulos_colocados.append((xm, ym))
                dx = abs(p['x1'] - p['x0'])
                dy = abs(p['y1'] - p['y0'])
                rotacao = 90 if dy > dx else 0

                ax.text(
                    xm, ym, f"{p['carga']:.1f}",
                    fontsize=tam_fonte, fontweight='bold', color='black',
                    ha='center', va='center', rotation=rotacao,
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='white', edgecolor='black', linewidth=0.6, alpha=0.92)
                )

        cbar = plt.colorbar(img, pad=0.02)
        cbar.set_label('Carga Linear no Radier (kN/m)', fontsize=11)
        ax.set_title('Planta Cotada de Cargas no Radier (kN/m)', fontsize=13, weight='bold')
        ax.set_xlabel('Coordenada X (m)', fontsize=11)
        ax.set_ylabel('Coordenada Y (m)', fontsize=11)
        ax.grid(color='white', linestyle='--', alpha=0.3)
        fig.tight_layout()

        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format='png', dpi=250)
        plt.close(fig)

    return pd.DataFrame(relatorio), img_buffer, linhas_consolidadas

def criar_visualizador_3d(linhas_paredes):
    """Gera o modelo BIM 3D interativo com mapa térmico de cargas nas paredes"""
    fig = go.Figure()
    cargas = [p['carga'] for p in linhas_paredes]
    c_min = min(cargas) if cargas else 0.0
    c_max = max(cargas) if cargas else 50.0

    altura_visual = 2.80

    for p in linhas_paredes:
        x0, y0 = p['x0'], p['y0']
        x1, y1 = p['x1'], p['y1']
        carga = p['carga']

        # Geometria do prisma 3D da parede
        x_mesh = [x0, x1, x1, x0, x0, x1, x1, x0]
        y_mesh = [y0, y1, y1, y0, y0, y1, y1, y0]
        z_mesh = [0.0, 0.0, altura_visual, altura_visual, 0.0, 0.0, altura_visual, altura_visual]

        i_faces = [7, 0, 0, 0, 4, 4, 6, 6, 4, 0, 3, 2]
        j_faces = [3, 4, 1, 2, 5, 6, 5, 2, 0, 1, 6, 3]
        k_faces = [0, 7, 2, 3, 6, 7, 1, 1, 5, 5, 7, 6]

        hover_texto = (
            f"<b>{p['nome']}</b><br>"
            f"Comprimento: {p['comprimento']:.2f} m<br>"
            f"<b>Carga no Radier: {carga:.2f} kN/m</b><br>"
            f"Carga da Laje: {p['carga_laje']:.2f} kN/m<br>"
            f"Peso Próprio: {p['carga_pp']:.2f} kN/m"
        )

        fig.add_trace(go.Mesh3d(
            x=x_mesh, y=y_mesh, z=z_mesh,
            i=i_faces, j=j_faces, k=k_faces,
            intensity=[carga] * 8,
            colorscale='Jet',
            cmin=c_min,
            cmax=c_max,
            showscale=False,
            hoverinfo='text',
            text=hover_texto,
            opacity=0.92
        ))

        # Arestas de contorno em preto
        fig.add_trace(go.Scatter3d(
            x=[x0, x1, x1, x0, x0],
            y=[y0, y1, y1, y0, y0],
            z=[0, 0, altura_visual, altura_visual, 0],
            mode='lines',
            line=dict(color='black', width=3),
            hoverinfo='skip',
            showlegend=False
        ))

    # Barra de escala de cores do 3D
    fig.add_trace(go.Scatter3d(
        x=[None], y=[None], z=[None],
        mode='markers',
        marker=dict(
            colorscale='Jet',
            cmin=c_min,
            cmax=c_max,
            color=[c_min, c_max],
            colorbar=dict(title=dict(text="Carga Linear (kN/m)", side="right"), thickness=18, len=0.8),
            showscale=True
        ),
        showlegend=False
    ))

    fig.update_layout(
        title="<b>Modelo Tridimensional Interativo de Cargas nas Paredes</b>",
        scene=dict(
            xaxis=dict(title='X (m)', backgroundcolor="rgb(240, 243, 246)", gridcolor="white"),
            yaxis=dict(title='Y (m)', backgroundcolor="rgb(240, 243, 246)", gridcolor="white"),
            zaxis=dict(title='Z (m)', backgroundcolor="rgb(240, 243, 246)", gridcolor="white"),
            aspectmode='data',
            camera=dict(eye=dict(x=-1.4, y=-1.8, z=1.3))
        ),
        margin=dict(l=0, r=0, b=0, t=40)
    )
    return fig

# --- EXECUÇÃO ---
if arquivo_ifc is not None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ifc") as tmp_file:
        tmp_file.write(arquivo_ifc.read())
        tmp_caminho = tmp_file.name

    try:
        df, img_buffer, linhas_3d = processar_calculo_completo(
            tmp_caminho, peso_alvenaria, peso_concreto, carga_acidental_laje,
            vao_medio_laje, resolucao_grid, exibir_rotulos_2d, 
            comp_minimo_rotulo, dist_min_entre_rotulos, tamanho_fonte
        )

        st.subheader("Resultados Analíticos e Tributários das Cargas no Radier")
        if not df.empty:
            st.dataframe(df, use_container_width=True)

            tab1, tab2 = st.tabs(["Visualizador 3D Interativo", "Planta Técnica 2D"])

            with tab1:
                if TEM_PLOTLY and linhas_3d:
                    fig_3d = criar_visualizador_3d(linhas_3d)
                    st.plotly_chart(fig_3d, use_container_width=True)
                elif not TEM_PLOTLY:
                    st.info("Para ativar o visualizador 3D, adicione `plotly` ao ficheiro requirements.txt.")

            with tab2:
                if img_buffer:
                    st.image(img_buffer, caption="Planta de Cargas Cotada com Prevenção de Sobreposição (kN/m)")

            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Tabela de Cargas')
                if img_buffer:
                    ws = writer.book.create_sheet('Mapa Visual 2D')
                    img_buffer.seek(0)
                    ws.add_image(OpenpyxlImage(img_buffer), 'B2')

            st.download_button(
                label="📥 Descarregar Folha de Cálculo Estrutural (.xlsx)",
                data=excel_buffer.getvalue(),
                file_name="cargas_radier_detalhadas.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.error("Não foram encontradas paredes válidas no piso térreo.")

    except Exception as e:
        st.error(f"Erro no processamento: {e}")
    finally:
        os.remove(tmp_caminho)
